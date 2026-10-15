"""Entity lookup for matching index headings.

An entity exists only if it is in bahai-data. Two curated files add what is approved but
not yet applied there:
- curated/new_entities.jsonl: new entities, as draft records in bahai-data's format
- curated/new_labels.csv: extra names (id,label,lang,note) for entities that exist in bahai-data
"""
import csv
import json
import re
import sys
import unicodedata
from common import BAHAI_DATA_DIR, NEW_ENTITIES_JSONL, NEW_LABELS_CSV

AUTHORITY_FILES = ["people", "places", "events", "groups", "subjects", "circular_instructions"]

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

def _read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]

def _label_value(label):
    """Labels are normally {"value": ..., "lang": ...}; a few records use a plain string."""
    return label.get('value') if isinstance(label, dict) else label

def _record_names(record):
    names = [_label_value(record.get('preferred_label'))]
    names += [_label_value(v) for v in record.get('variant_labels') or []]
    return [n for n in names if n]

def load_entities():
    """Loads bahai-data entities and the curated pending files.
    Returns (existing, pending, extra_labels): existing and pending map id -> record;
    extra_labels is a list of (id, label) for entities that exist."""
    if not BAHAI_DATA_DIR.is_dir():
        sys.exit(f"[!] bahai-data not found at {BAHAI_DATA_DIR}. Check it out as a sibling of bahai-index.")

    existing = {}
    for name in AUTHORITY_FILES:
        for r in _read_jsonl(BAHAI_DATA_DIR / f"{name}.jsonl"):
            if r.get('record_type') != 'redirect':
                existing[r['id']] = r
    # Works are not matched by title (titles like "Baghdad" cause false matches), but
    # their IDs count as existing so curated labels can point to them.
    with open(BAHAI_DATA_DIR / "works.json", encoding='utf-8') as f:
        work_ids = {w['id'] for w in json.load(f) if w.get('id')}

    pending = {}
    if NEW_ENTITIES_JSONL.exists():
        for r in _read_jsonl(NEW_ENTITIES_JSONL):
            if r['id'] in existing or r['id'] in work_ids:
                print(f"  [!] {r['id']} is already in bahai-data; remove it from {NEW_ENTITIES_JSONL.name}.")
                continue
            pending[r['id']] = r

    extra_labels = []
    if NEW_LABELS_CSV.exists():
        with open(NEW_LABELS_CSV, encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                if row['id'] in existing or row['id'] in work_ids:
                    extra_labels.append((row['id'], row['label']))
                else:
                    print(f"  [!] {NEW_LABELS_CSV.name}: {row['id']} is not in bahai-data; label '{row['label']}' skipped.")

    return existing, pending, extra_labels

def build_lookup():
    """Maps each normalized name to (id, status), where status is 'exists' (in bahai-data)
    or 'pending' (only in new_entities.jsonl). Names claimed by two different entities are
    reported and left out, so they stay unmatched and go to review."""
    existing, pending, extra_labels = load_entities()

    claims = {}  # key -> {id: status}
    def claim(name, uid, status):
        for key in match_keys(name):
            claims.setdefault(key, {})[uid] = status

    for uid, r in existing.items():
        for name in _record_names(r):
            claim(name, uid, 'exists')
    for uid, label in extra_labels:
        claim(label, uid, 'exists')
    for uid, r in pending.items():
        for name in _record_names(r):
            claim(name, uid, 'pending')

    lookup, ambiguous = {}, {}
    for key, ids in claims.items():
        if len(ids) == 1:
            lookup[key] = next(iter(ids.items()))
        else:
            ambiguous[key] = sorted(ids)

    print(f"Entities: {len(existing)} in bahai-data, {len(pending)} pending; "
          f"{len(extra_labels)} curated labels; {len(lookup)} names")
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
