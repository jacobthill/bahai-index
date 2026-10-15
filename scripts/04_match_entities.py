"""Step 04: matches each index entry to an entity and writes -text-with-ids.csv.

An entry is matched on its full text first (heading + subentry, which can name a specific
event), then on its heading alone. Matching uses bahai-data plus the decisions in
curated/review.csv (see scripts/entities.py). Unmatched headings are reviewed in
curated/review.csv, built by scripts/review.py; this step only reports how many there are.
"""
import sys
import pandas as pd
from common import parse_volume
from entities import build_lookup, resolve_entry

def match_entries(paths):
    csv_input = paths.final_csv
    if not csv_input.exists():
        print(f"[!] Error: Could not find {csv_input}. Did you run step 03 first?")
        sys.exit(1)

    print("Building lookup from bahai-data and curated files...")
    lookup = build_lookup()

    print(f"Reading CSV: {csv_input.name}...")
    df = pd.read_csv(csv_input)

    print("Resolving IDs...")
    resolved = [resolve_entry(r.main_subject, r.sub_subject, r.full_subject, lookup) for r in df.itertuples()]
    df['id'] = [r[0] for r in resolved]
    # 'exists': in bahai-data; 'pending': approved in review.csv, not yet in bahai-data
    df['id_status'] = [r[1] for r in resolved]
    # 'full_subject' or 'heading': which part of the entry matched
    df['id_matched_on'] = [r[2] for r in resolved]

    id_cols = ['id', 'id_status', 'id_matched_on']
    df = df[id_cols + [c for c in df.columns if c not in id_cols]]
    df.to_csv(paths.with_ids_csv, index=False)

    entries = df[df['type'] != 'cross_reference']
    exists = (entries['id_status'] == 'exists').sum()
    pending = (entries['id_status'] == 'pending').sum()
    unmatched = entries[entries['id'].isna()]
    print(f"\n--- Matching Complete ---")
    print(f"Entries:          {len(entries)}")
    print(f"Matched:          {exists + pending} ({(exists + pending) / len(entries):.1%})")
    print(f"  in bahai-data:  {exists}")
    print(f"  pending:        {pending} (approved in curated/review.csv, not yet in bahai-data)")
    print(f"  on full entry:  {(entries['id_matched_on'] == 'full_subject').sum()}, on heading: {(entries['id_matched_on'] == 'heading').sum()}")
    print(f"Unmatched:        {len(unmatched)} entries under {unmatched['main_subject'].nunique()} headings (review them in curated/review.csv)")
    print(f"Output:           {paths.with_ids_csv}")

if __name__ == "__main__":
    paths = parse_volume("Match index entries to entities.")
    match_entries(paths)
