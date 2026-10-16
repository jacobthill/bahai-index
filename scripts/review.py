"""Builds the review sheet (curated/review.csv) and drafts records for the current batch.

    uv run python scripts/review.py build [--batch 50] [--no-llm]

For every heading in either volume that doesn't match an entity, and every subentry that may be
an entity of its own (e.g. "Nasir al-Din Shah Qajar: assassination of"), the sheet gets a row with:
  - likely existing entities (candidates) and the index's cross-references,
  - a suggestion: link:<id>, new:<id> (the prefix gives the type), or ignore.
Your columns are 'verdict' and 'notes'; they are never changed. Rebuilding only adds new rows and
refreshes the script's columns (counts, candidates, cross-references, status).

--batch N picks the N rows that most need work (undecided, or approved as new without a complete
draft), ordered by mentions, and asks Gemini to draft a full bahai-data record for each new entity
into curated/drafts.jsonl. Existing drafts are never overwritten; for an incomplete one, only its
empty fields are filled in.
"""
import argparse
import csv
import difflib
import json
import re
import time
from collections import Counter, defaultdict
import pandas as pd
from common import (volume_paths, VOLUMES, REVIEW_CSV, DRAFTS_JSONL, DATA_DIR,
                    get_gemini_client, cached_generate_json)
from entities import (REVIEW_COLUMNS, build_lookup, resolve_entry, load_review, decision,
                      load_existing, load_drafts, record_names, normalize_for_match, flip_comma_name)

LLM_CACHE = DATA_DIR / "cache" / "review_llm.jsonl"
PREFIXES = {"person": "person_", "place": "place_", "group": "group_", "event": "event_",
            "subject": "subject_", "work": "work_"}
MAX_EVIDENCE = 4          # excerpts per draft
EVIDENCE_CHARS = 1500     # characters per excerpt

# --- Gathering entries ---

def load_entries():
    """All index entries from step 03 output, both volumes, with their match."""
    lookup = build_lookup()
    frames = []
    for vol in VOLUMES:
        df = pd.read_csv(volume_paths(vol).final_csv)
        df = df[df['type'] != 'cross_reference'].copy()
        df['volume'] = vol
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    res = [resolve_entry(r.main_subject, r.sub_subject, r.full_subject, lookup) for r in df.itertuples()]
    df['id'] = [r[0] for r in res]
    df['heading_key'] = df['main_subject'].map(normalize_for_match)
    df['full_key'] = df['full_subject'].map(normalize_for_match)
    df['has_sub'] = df['sub_subject'].fillna("").astype(str).str.strip().ne("")
    return df

def most_common(values):
    return Counter(values).most_common(1)[0][0]

def summarize(group):
    """Volumes, mention count, and frequent subentries for a group of entries."""
    subs = Counter(s for s in group['sub_subject'].dropna() if str(s).strip())
    return {
        "volumes": ",".join(sorted(group['volume'].unique())),
        "mentions": str(len(group)),
        "subentries": "; ".join(s for s, _ in subs.most_common(6)),
    }

# --- Candidates and cross-references ---

def build_name_index(rows=()):
    """(key, id, label) for every name of every entity in bahai-data and in the drafts, and for
    other review rows' suggested new entities (so spelling variants across volumes, e.g.
    'Biutsov, Evgenii' and 'Biutsov, Yevgenii', show each other as candidates)."""
    existing, _ = load_existing()
    index = []
    for row in rows:
        if row['suggestion'].startswith('new:'):
            uid = row['suggestion'].split(':', 1)[1]
            for key in {normalize_for_match(row['text']), normalize_for_match(flip_comma_name(row['text']))}:
                if key:
                    index.append((key, uid, f"{status_of(row)} row: {row['text']}"))
    for uid, r in list(existing.items()) + list(load_drafts().items()):
        label = record_names(r)[0] if record_names(r) else uid
        for name in record_names(r):
            for key in {normalize_for_match(name), normalize_for_match(flip_comma_name(name))}:
                if key:
                    index.append((key, uid, label))
    return index

_candidate_cache = {}

# Titles and community words shared by many names; they don't suggest the same entity
COMMON_WORDS = {"mirza", "sayyid", "shaykh", "mulla", "ustad", "haji", "bahais", "babis", "bahai",
                "religion", "russian", "persian", "iranian", "general", "prince", "baron", "unidentified",
                "muhammad", "abdulbaha", "bahaullah", "khanum", "sultan", "governor", "consul"}

def find_candidates(text, name_index, n=3):
    """Likely existing entities for a heading, as [(id, label, score)], best first."""
    if text in _candidate_cache:
        return _candidate_cache[text]
    keys = {normalize_for_match(text), normalize_for_match(flip_comma_name(text))} - {""}
    distinctive = {w for k in keys for w in k.split() if len(w) >= 6 and w not in COMMON_WORDS}
    scores = {}
    for key, uid, label in name_index:
        s = max(difflib.SequenceMatcher(None, k, key).ratio() for k in keys)
        # A shared distinctive word (e.g. a surname) is worth showing even when the rest differs
        if s < 0.8 and distinctive & set(key.split()):
            s = max(s, 0.7)
        # Short names match by chance ("Islam" ~ "Jesus"), so they need a closer match
        if min(len(k) for k in keys) < 8 and s < 0.85:
            continue
        if s >= 0.7 and s > scores.get(uid, (0, ""))[0]:
            scores[uid] = (s, label)
    best = sorted(scores.items(), key=lambda kv: -kv[1][0])[:n]
    _candidate_cache[text] = [(uid, label, score) for uid, (score, label) in best]
    return _candidate_cache[text]

def load_cross_refs():
    """Notes per normalized heading, from cross_references.py output."""
    notes = defaultdict(list)
    for vol in VOLUMES:
        path = volume_paths(vol).cross_refs_csv
        if not path.exists():
            continue
        for r in pd.read_csv(path).itertuples():
            words = {"see": "see", "see_also": "see also", "see_under": "see under"}[r.kind]
            notes[normalize_for_match(r.heading)].append(f"index v{vol}: {words} {r.target}")
            notes[normalize_for_match(r.target)].append(f"index v{vol}: '{r.heading}' {words} here")
    return notes

# --- Suggestions (Gemini, cached) ---

def classify(client, items, subentries=False):
    """Asks Gemini for a category per heading (or whether each subentry is its own entity).
    Returns {text: category}, where category is one of PREFIXES, 'describes' or 'ignore'."""
    results = {}
    for i in range(0, len(items), 50):
        chunk = items[i:i + 50]
        if subentries:
            prompt = f"""You are a historian building a knowledge graph from a book index (Baha'i and Russian
archival history, 19th–20th c.). Each item is "Heading: subentry". Most subentries only describe what the
page says about the heading (e.g. "marriage of", "letters of", "and Kitabi Aqdas") and are NOT separate
entities: answer "describes". Answer a category only when the subentry names a distinct thing worth its own
record: an event (e.g. "execution of", "assassination of", a conference or massacre), a group (e.g. "Baha'is of"),
a place, a work, or a subject.

Respond ONLY with JSON: {{"<item exactly as given>": "describes" | "event" | "group" | "place" | "work" | "subject"}}

Items: {json.dumps(chunk, ensure_ascii=False)}"""
        else:
            prompt = f"""You are a historian building a knowledge graph from a book index (Baha'i and Russian
archival history, 19th–20th c.). Classify each index heading as one of: person, place, group, event,
subject, work. Use "ignore" only for headings that are not worth an entity at all.

Respond ONLY with JSON: {{"<heading exactly as given>": "person" | "place" | "group" | "event" | "subject" | "work" | "ignore"}}

Headings: {json.dumps(chunk, ensure_ascii=False)}"""
        try:
            results.update(cached_generate_json(client, prompt, LLM_CACHE))
        except Exception as e:
            print(f"  [!] Gemini classification failed for {len(chunk)} items: {type(e).__name__}: {e}")
    return results

def slug(text):
    """A readable ID part: 'Babis/Baha'is: persecution of' -> 'persecution_of_babis_bahais',
    'Ashgabat (Askhabad): Baha'is of' -> 'bahais_of_ashgabat', 'Rozen, Baron Viktor' -> 'baron_viktor_rozen'."""
    text = re.sub(r'\([^)]*\)', ' ', text).replace('/', ' ')
    if ': ' in text:
        heading, sub = (t.strip() for t in text.split(': ', 1))
        heading = flip_comma_name(heading) if heading.count(',') == 1 else heading
        sub = re.sub(r'^and\s+', '', sub)
        text = f"{sub} {heading}" if re.search(r'\b(of|by|in|to|on)$', sub) else f"{heading} {sub}"
    elif text.count(',') == 1:
        text = flip_comma_name(text)
    text = normalize_for_match(text)
    return re.sub(r'\s+', '_', text)[:60].strip('_')

def mint_id(category, text, taken):
    """A new ID: prefix + simplified name, made unique against everything already used."""
    base = PREFIXES[category] + slug(text)
    uid, n = base, 2
    while uid in taken:
        uid, n = f"{base}_{n}", n + 1
    taken.add(uid)
    return uid

# --- Drafting full records ---

REQUIRED = {
    "person": '"birth_date": "YYYY-MM-DD" or null, "death_date": "YYYY-MM-DD" or null',
    "place": '"location": {"latitude": number, "longitude": number}',
    "group": '"type": short type (e.g. "Religious Community", "Organization"), "start_date": ... or null, "end_date": ... or null',
    "event": '"start_date": "YYYY-MM-DD" (or YYYY-MM / YYYY) or null, "end_date": ... or null',
    "subject": '"type": short type (e.g. "Religious Law", "Historical Event/Condition")',
    "work": '"author": {"id": ..., "label": ...} or null',
}

MACRONS = str.maketrans("āīūĀĪŪ", "áíúÁÍÚ")

def to_bahai_style(value):
    """Long vowels with acute accents, as in the Bahá'í system (Ṭihrán, not Ṭihrān)."""
    if isinstance(value, str):
        return value.translate(MACRONS)
    if isinstance(value, list):
        return [to_bahai_style(v) for v in value]
    if isinstance(value, dict):
        return {k: to_bahai_style(v) for k, v in value.items()}
    return value

def needs_fields(record):
    """True if a draft is missing fields bahai-data requires (or a place lacks coordinates)."""
    if not record.get('description'):
        return True
    if record.get('record_type') == 'place':
        loc = record.get('location') or {}
        return loc.get('latitude') is None or loc.get('longitude') is None
    return False

def evidence_for(entries, row):
    """Excerpts and pages for a review row, for drafting and provenance."""
    key = normalize_for_match(row['text'])
    sel = entries[(entries['full_key'] == key) if row['kind'] == 'subentry' else (entries['heading_key'] == key)]
    sel = sel[sel['presentation_text'].fillna("").str.len() > 0]
    excerpts = [f"[vol. {r.volume}, p. {r.page}] {str(r.presentation_text)[:EVIDENCE_CHARS]}" for r in sel.head(MAX_EVIDENCE).itertuples()]
    pages = [{"volume": r.volume, "page": str(r.page)} for r in sel.itertuples()]
    return excerpts, pages

def draft_record(client, uid, row, entries, participant_id, existing_draft):
    category = next(c for c, p in PREFIXES.items() if uid.startswith(p))
    excerpts, pages = evidence_for(entries, row)
    names = record_names(existing_draft) if existing_draft else [row['text']]
    participant = f'\nThe event involves the entity {participant_id}; include {{"type": "has_participant", "related_id": "{participant_id}"}} in relationships.' if category == "event" and participant_id else ""
    prompt = f"""You are a historian writing an authority record for a knowledge graph, from passages of
Shahvar et al., The Baha'is of Iran, Transcaspia and the Caucasus (index entry: "{row['text']}").
Entity id: {uid} (type: {category}). Known names: {json.dumps(names, ensure_ascii=False)}.{participant}

Write ONE JSON object with exactly these keys:
"preferred_label": {{"value": scholarly form of the name with diacritics if known, "lang": "en"}},
"variant_labels": list of {{"value": ..., "lang": BCP 47 code}} (include the index spelling, and Persian/Arabic/Russian forms if you are confident),
"description": {{"value": 1–3 factual sentences, "lang": "en"}},
{REQUIRED[category]},
"relationships": list of {{"type": snake_case relation, "related_id": an id}} (only ids given above; otherwise []),
"draft_notes": a short note on anything uncertain or taken from general knowledge rather than the passages (e.g. coordinates, dates).

Write names in the Bahá'í transliteration system used by the existing records, with acute accents and
underdots, not macrons (e.g. Baháʼuʼlláh, Bábí, Mírzá, Ṭihrán, ʻAbdu'l-Bahá).
Use the passages first; use well-established general knowledge only where they are silent, and say so in draft_notes.
Use null for anything unknown. Do not invent dates.

Passages:
{chr(10).join(excerpts) if excerpts else "(no passages available)"}"""
    record = to_bahai_style(cached_generate_json(client, prompt, LLM_CACHE))
    record = dict({"id": uid, "record_type": category}, **record)
    record["status"] = "draft"
    record["draft_sources"] = pages
    return record

def merge_draft(existing, new):
    """Fills only the empty fields of an existing draft; your edits are kept."""
    merged = dict(existing)
    for key, value in new.items():
        if merged.get(key) in (None, "", [], {}) or (key == "location" and None in (merged.get(key) or {}).values()):
            merged[key] = value
    names = {normalize_for_match(n) for n in record_names(existing)}
    merged["variant_labels"] = list(existing.get("variant_labels") or []) + [
        v for v in new.get("variant_labels") or [] if normalize_for_match(v.get("value") if isinstance(v, dict) else v) not in names]
    return merged

# --- Build ---

def status_of(row):
    if row['status'] == 'applied':
        return 'applied'
    d = decision(row)
    return 'proposed' if d is None else ('ignored' if d == 'ignore' else 'approved')

def build(batch_size, use_llm):
    client = get_gemini_client() if use_llm else None
    entries = load_entries()
    rows = load_review()
    by_key = {(r['kind'], normalize_for_match(r['text'])): r for r in rows}
    existing, work_ids = load_existing()
    drafts = load_drafts()
    taken = set(existing) | work_ids | set(drafts) | {
        r['suggestion'].split(':', 1)[1] for r in rows if ':' in r['suggestion']}

    # Items needing a row: unmatched headings, and subentries that may be entities themselves
    unmatched = entries[entries['id'].isna()]
    heading_items = {k: g for k, g in unmatched.groupby('heading_key') if k}
    sub_groups = {k: g for k, g in entries[entries['has_sub']].groupby('full_key') if k}
    new_subs = [most_common(g['full_subject']) for k, g in sub_groups.items() if ('subentry', k) not in by_key]
    new_headings = [most_common(g['main_subject']) for k, g in heading_items.items() if ('heading', k) not in by_key]

    print(f"Review sheet: {len(rows)} existing rows; {len(new_headings)} new headings, "
          f"{len(new_subs)} subentries to classify")
    head_cats = classify(client, new_headings) if use_llm and new_headings else {}
    sub_cats = classify(client, new_subs, subentries=True) if use_llm and new_subs else {}

    name_index = build_name_index(rows)
    cross_refs = load_cross_refs()

    for text in new_headings:
        cands = find_candidates(text, name_index)
        cat = head_cats.get(text)
        if cands and cands[0][2] >= 0.92:
            suggestion = f"link:{cands[0][0]}"
        elif cat == 'ignore':
            suggestion = 'ignore'
        elif cat in PREFIXES:
            suggestion = f"new:{mint_id(cat, text, taken)}"
        else:
            suggestion = ""
        row = dict({c: "" for c in REVIEW_COLUMNS}, kind='heading', text=text, suggestion=suggestion)
        rows.append(row); by_key[('heading', normalize_for_match(text))] = row
    for text in new_subs:
        cat = sub_cats.get(text)
        if cat not in PREFIXES:
            continue   # describes its heading: no row needed
        row = dict({c: "" for c in REVIEW_COLUMNS}, kind='subentry', text=text,
                   suggestion=f"new:{mint_id(cat, text, taken)}")
        rows.append(row); by_key[('subentry', normalize_for_match(text))] = row

    # Refresh the script's columns on every row; verdict and notes are left alone
    name_index = build_name_index(rows)   # now including the rows just added
    _candidate_cache.clear()
    for row in rows:
        key = normalize_for_match(row['text'])
        group = entries[(entries['full_key'] == key) if row['kind'] == 'subentry' else (entries['heading_key'] == key)]
        info = summarize(group) if len(group) else {"volumes": "", "mentions": "0", "subentries": ""}
        row.update(info)
        if row['kind'] == 'subentry':
            row['subentries'] = ""
        if status_of(row) == 'proposed':
            own = row['suggestion'].split(':', 1)[1] if ':' in row['suggestion'] else None
            row['candidates'] = "; ".join(f"{u} ({l}, {s:.2f})" for u, l, s in
                                          find_candidates(row['text'], name_index, n=4) if u != own)
        row['cross_refs'] = "; ".join(dict.fromkeys(cross_refs.get(key, [])))
        row['status'] = status_of(row)

    # Decisions that attach to no index entry, and whose entity no entry matches through any other
    # name either: usually the text changed after a parser fix. (A name that isn't printed in
    # these indexes, like 'Koran', is fine as long as its entity matches something.)
    matched_ids = set(entries['id'].dropna())
    orphaned = [r for r in rows if r['mentions'] == '0' and (
        r['status'] == 'ignored' or (r['status'] == 'approved' and (decision(r) or ':').split(':', 1)[1] not in matched_ids))]
    if orphaned:
        print(f"  [!] {len(orphaned)} decided rows match no index entry; update their text or remove them:")
        for r in orphaned:
            print(f"      {r['kind']}: '{r['text']}' ({r['verdict']}: {r['suggestion']})")

    # Batch: the rows that most need work
    def needs_work(row):
        d = decision(row) or row['suggestion']
        if row['status'] == 'proposed':
            return True
        return row['status'] == 'approved' and d.startswith('new:') and (
            d.split(':', 1)[1] not in drafts or needs_fields(drafts[d.split(':', 1)[1]]))
    numbered = [int(r['batch']) for r in rows if r['batch'].isdigit()]
    batch_no = str(max(numbered, default=0) + 1)
    candidates = sorted([r for r in rows if needs_work(r) and not r['batch']], key=lambda r: -int(r['mentions'] or 0))
    batch = candidates[:batch_size] if batch_size else []
    for row in batch:
        row['batch'] = batch_no

    # Drafts for new entities in the batch, and for earlier batches whose draft failed
    to_draft = batch + [r for r in rows if r['batch'] and r not in batch and needs_work(r)]
    drafted = 0
    if use_llm and to_draft:
        lookup = build_lookup()
        for row in to_draft:
            d = decision(row) or row['suggestion']
            if not d.startswith('new:'):
                continue
            uid = d.split(':', 1)[1]
            if uid in drafts and not needs_fields(drafts[uid]):
                continue
            participant = None
            if row['kind'] == 'subentry':
                participant = resolve_entry(row['text'].split(': ', 1)[0], None, None, lookup)[0]
            try:
                new = draft_record(client, uid, row, entries, participant, drafts.get(uid))
            except Exception as e:
                print(f"  [!] Draft failed for {uid}: {type(e).__name__}: {e}")
                continue
            drafts[uid] = merge_draft(drafts[uid], new) if uid in drafts else new
            drafted += 1
            time.sleep(0.5)

    # Write: undecided first, then by mentions
    order = {'proposed': 0, 'approved': 1, 'ignored': 2, 'applied': 3}
    rows.sort(key=lambda r: (order[r['status']], r['batch'] != batch_no, -int(r['mentions'] or 0)))
    with open(REVIEW_CSV, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS); w.writeheader(); w.writerows(rows)
    DRAFTS_JSONL.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in drafts.values()), encoding='utf-8')

    counts = Counter(r['status'] for r in rows)
    print(f"Wrote {REVIEW_CSV.name}: {len(rows)} rows ({dict(counts)})")
    print(f"Batch {batch_no}: {len(batch)} rows; {drafted} drafts written to {DRAFTS_JSONL.name}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the review sheet and draft records.")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--batch", type=int, default=50, help="rows to select and draft (0: none)")
    parser.add_argument("--no-llm", action="store_true", help="skip Gemini (no suggestions or drafts)")
    args = parser.parse_args()
    build(args.batch, not args.no_llm)
