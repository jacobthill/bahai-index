"""Entity lookup for matching index headings.

An entity exists only if it is in bahai-data. Two curated files add your decisions that are
not yet applied there:
- curated/review.csv: one row per heading (or subentry) that needed a decision. The verdict
  column is yours: blank, 'approve' (accept the suggestion), 'link:<id>', 'new:<id>', or 'ignore'.
- curated/drafts.jsonl: draft records in bahai-data's format for new entities.

An approved link makes the row's text a name of an existing entity right away. An approved
new entity counts as 'pending' until it is added to bahai-data.
"""
import csv
import json
import re
import sys
import unicodedata
from common import BAHAI_DATA_DIR, REVIEW_CSV, DRAFTS_JSONL

AUTHORITY_FILES = ["people", "places", "events", "groups", "subjects", "circular_instructions"]

REVIEW_COLUMNS = ["kind", "text", "volumes", "mentions", "subentries", "candidates", "cross_refs",
                  "suggestion", "verdict", "notes", "status", "batch"]

def flip_comma_name(name):
    """Flips 'Last, First' to 'First Last'. If no comma, returns original."""
    if name is None or ',' not in str(name):
        return str(name)
    parts = [p.strip() for p in str(name).split(',')]
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    return str(name)

def normalize_for_match(text):
    """Aggressively flattens strings (removes accents, hyphens, and quotes)."""
    if text is None or text != text:  # None or NaN
        return ""
    text = str(text).lower()
    text = re.sub(r"['‘’ʻʼ`\-]", "", text)
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def match_keys(name):
    """The normalized forms a name is matched under: as written, and with 'Last, First' flipped."""
    keys = {normalize_for_match(name)}
    if ',' in str(name):
        keys.add(normalize_for_match(flip_comma_name(name)))
    return {k for k in keys if k}

def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]

def label_value(label):
    """Labels are normally {"value": ..., "lang": ...}; a few records use a plain string."""
    return label.get('value') if isinstance(label, dict) else label

def record_names(record):
    names = [label_value(record.get('preferred_label'))]
    names += [label_value(v) for v in record.get('variant_labels') or []]
    return [n for n in names if n]

def load_review():
    """Returns the review sheet as a list of dicts (empty if it doesn't exist yet)."""
    if not REVIEW_CSV.exists():
        return []
    with open(REVIEW_CSV, encoding='utf-8', newline='') as f:
        return [{c: (row.get(c) or "") for c in REVIEW_COLUMNS} for row in csv.DictReader(f)]

def decision(row):
    """The row's effective decision: 'link:<id>', 'new:<id>', 'ignore', or None if undecided."""
    verdict = row['verdict'].strip()
    if not verdict:
        return None
    if verdict.lower() == 'approve':
        verdict = row['suggestion'].strip()
    if verdict == 'ignore' or re.match(r'^(link|new):\w+$', verdict):
        return verdict
    print(f"  [!] review.csv: unrecognized verdict '{row['verdict']}' for '{row['text']}' (ignored)")
    return None

def load_existing():
    """bahai-data entities (id -> record, redirects excluded) and the set of work IDs."""
    if not BAHAI_DATA_DIR.is_dir():
        sys.exit(f"[!] bahai-data not found at {BAHAI_DATA_DIR}. Check it out as a sibling of bahai-index.")
    existing = {}
    for name in AUTHORITY_FILES:
        for r in read_jsonl(BAHAI_DATA_DIR / f"{name}.jsonl"):
            if r.get('record_type') != 'redirect':
                existing[r['id']] = r
    with open(BAHAI_DATA_DIR / "works.json", encoding='utf-8') as f:
        work_ids = {w['id'] for w in json.load(f) if w.get('id')}
    return existing, work_ids

def load_drafts():
    return {r['id']: r for r in read_jsonl(DRAFTS_JSONL)} if DRAFTS_JSONL.exists() else {}

def build_lookup():
    """Maps each normalized name to (id, status), where status is 'exists' (in bahai-data)
    or 'pending' (approved in review.csv, not yet in bahai-data). Names claimed by two
    different entities are reported and left out, so they stay unmatched for review."""
    existing, work_ids = load_existing()
    drafts = load_drafts()
    known = set(existing) | work_ids

    claims = {}  # key -> {id: status}
    def claim(name, uid, status):
        for key in match_keys(name):
            claims.setdefault(key, {})[uid] = status

    # Works are not matched by title (titles like "Baghdad" cause false matches); they are
    # matched only through approved review rows.
    for uid, r in existing.items():
        for name in record_names(r):
            claim(name, uid, 'exists')

    approved_links = approved_new = 0
    for row in load_review():
        d = decision(row)
        if not d or d == 'ignore':
            continue
        kind, uid = d.split(':', 1)
        status = 'exists' if uid in known else 'pending'
        if kind == 'link' and uid not in known and uid not in drafts:
            print(f"  [!] review.csv: '{row['text']}' links to unknown id {uid} (skipped)")
            continue
        claim(row['text'], uid, status)
        if kind == 'new' and uid in drafts and status == 'pending':
            for name in record_names(drafts[uid]):
                claim(name, uid, 'pending')
        approved_links += kind == 'link'
        approved_new += kind == 'new'

    lookup, ambiguous = {}, {}
    for key, ids in claims.items():
        if len(ids) == 1:
            lookup[key] = next(iter(ids.items()))
        else:
            ambiguous[key] = sorted(ids)

    print(f"Entities: {len(existing)} in bahai-data; review: {approved_links} approved links, "
          f"{approved_new} approved new entities; {len(lookup)} names")
    if ambiguous:
        print(f"  [!] {len(ambiguous)} names belong to more than one entity and are not matched:")
        for key, ids in list(ambiguous.items())[:10]:
            print(f"      '{key}': {', '.join(ids)}")
    return lookup

def resolve(subject, lookup):
    """Returns (id, status) for a heading, or (None, None) if no entity has that name.
    Tries the heading as written before the 'Last, First' flip."""
    for key in (normalize_for_match(subject), normalize_for_match(flip_comma_name(subject))):
        if key and key in lookup:
            return lookup[key]
    return (None, None)

def resolve_entry(main_subject, sub_subject, full_subject, lookup):
    """Tries an entry's full text first (heading + subentry, which can name a specific event),
    then the heading alone. Returns (id, status, matched_on)."""
    if sub_subject and str(sub_subject).strip() and str(sub_subject) != 'nan':
        uid, status = resolve(full_subject, lookup)
        if uid:
            return uid, status, 'full_subject'
    uid, status = resolve(main_subject, lookup)
    if uid:
        return uid, status, 'heading'
    return None, None, None
