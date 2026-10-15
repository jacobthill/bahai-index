import pandas as pd
import yaml
import re
import unicodedata
import sys
import json
import time
from common import parse_volume, get_gemini_client, generate_json, CATMAPPER_YAML

client = None  # Set in __main__ via get_gemini_client()

# --- PART 1: NORMALIZATION & MATCHING ---

def flip_comma_name(name):
    """Flips 'Last, First' to 'First Last'. If no comma, returns original."""
    if pd.isna(name) or ',' not in str(name):
        return str(name)
    parts = [p.strip() for p in str(name).split(',')]
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    return str(name)

def normalize_for_match(text):
    """Aggressively flattens strings (removes accents, hyphens, and quotes)."""
    if pd.isna(text):
        return ""
    text = str(text).lower()
    text = re.sub(r"['‘’ʻʼ`\-]", "", text)
    text = unicodedata.normalize('NFD', text)
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def build_lookup(yaml_path):
    """Builds a memory dictionary of all normalized YAML names and their ID."""
    with open(yaml_path, 'r', encoding='utf-8') as f:
        mappings = yaml.safe_load(f)

    lookup = {}
    def add_to_lookup(name_str, uid):
        if not name_str: return
        lookup[normalize_for_match(name_str)] = uid
        if ',' in str(name_str):
            lookup[normalize_for_match(flip_comma_name(name_str))] = uid

    for entry in mappings:
        uid = entry.get('id')
        if not uid: continue
        add_to_lookup(entry.get('preferred_name'), uid)
        for alt in entry.get('alternates', []):
            add_to_lookup(alt, uid)

    return lookup

# --- PART 2: LLM CATEGORIZATION ---

def mark_batch(subjects_list, category):
    """Labels every subject in a batch with a failure category and no draft ID."""
    return {sub: {"category": category, "draft_id": ""} for sub in subjects_list}

def get_categories_from_llm(subjects_list):
    """Batches unmatched subjects and asks Gemini to classify and generate an ID.
    On failure, subjects are labeled 'llm_unavailable' or 'llm_error' rather than given a guess."""
    if not client:
        return mark_batch(subjects_list, "llm_unavailable")

    prompt = f"""
    You are an expert historian and data taxonomist classifying index entries.
    Given the following list of historical index subjects, categorize each into one of these exact types:
    - person (use prefix: person_)
    - place (use prefix: place_)
    - group (use prefix: group_)
    - work (use prefix: work_)
    - event (use prefix: event_)
    - subject (use prefix: subject_)

    Then, create a normalized, snake_case draft ID using the prefix and a simple version of the name, enough to identify the entity.

    Respond ONLY with a valid JSON object where keys are the exact original subjects provided, and values are objects containing "category" and "draft_id".

    Example:
    {{
        "Mirza Abu'l-Fadl": {{"category": "person", "draft_id": "person_mirza_abul_fadl"}},
        "Convention of 1844": {{"category": "event", "draft_id": "event_convention_of_1844"}},
        "Ashgabat": {{"category": "place", "draft_id": "place_ashgabat"}}
    }}

    Subjects to classify:
    {json.dumps(subjects_list)}
    """

    try:
        return generate_json(client, prompt)
    except Exception as e:
        print(f"  [!] Error calling Gemini ({len(subjects_list)} subjects marked 'llm_error'): {type(e).__name__}: {e}")
        return mark_batch(subjects_list, "llm_error")

# --- PART 3: MAIN EXECUTION ---

def process_csv_with_ids(paths):
    yaml_path = CATMAPPER_YAML
    csv_input = paths.final_csv

    missing = [p for p in (yaml_path, csv_input) if not p.exists()]
    if missing:
        print("[!] Error: Could not find required files:")
        for p in missing:
            print(f"      {p}")
        sys.exit(1)

    # Refuse to overwrite existing outputs (the triage file may contain manual review work)
    csv_output = paths.with_ids_csv
    triage_output = paths.triage_csv
    existing = [p for p in (csv_output, triage_output) if p.exists()]
    if existing:
        print("[!] Refusing to run: these output files already exist and would be overwritten:")
        for p in existing:
            print(f"      {p}")
        print("    Move or rename them (especially if they contain manual review work), then re-run.")
        sys.exit(1)

    print("Building normalized lookup table from YAML...")
    lookup = build_lookup(yaml_path)

    print(f"Reading CSV: {csv_input.name}...")
    df = pd.read_csv(csv_input)

    def resolve_id(subject):
        if pd.isna(subject): return None
        search_val = normalize_for_match(subject)
        if search_val in lookup:
            return lookup[search_val]
        if ',' in str(subject):
            flipped_val = normalize_for_match(flip_comma_name(subject))
            if flipped_val in lookup:
                return lookup[flipped_val]
        return None

    print("Resolving IDs against YAML...")
    df['id'] = df['full_subject'].apply(resolve_id)

    # Reorder columns to put 'id' first
    cols = df.columns.tolist()
    if 'id' in cols:
        cols.insert(0, cols.pop(cols.index('id')))
        df = df[cols]

    # Save the main enriched CSV
    df.to_csv(csv_output, index=False)

    # --- TRIAGE GENERATION WITH GEMINI ---
    print("\nPreparing Triage Review Data...")
    unmatched_df = df[df['id'].isnull()]
    triage_counts = unmatched_df['full_subject'].value_counts().reset_index()
    triage_counts.columns = ['full_subject', 'frequency']

    unique_unmatched = triage_counts['full_subject'].tolist()
    print(f"Found {len(unique_unmatched)} unique unmatched entities. Asking Gemini for predictions...")

    # Batch LLM calls to prevent context overflow or massive JSON payloads
    chunk_size = 50
    llm_predictions = {}

    for i in range(0, len(unique_unmatched), chunk_size):
        chunk = unique_unmatched[i:i + chunk_size]
        print(f"  Processing batch {i//chunk_size + 1} of {(len(unique_unmatched) // chunk_size) + 1}...")

        batch_results = get_categories_from_llm(chunk)
        llm_predictions.update(batch_results)
        time.sleep(1.5)  # Rate limit protection

    # Map LLM results back to the triage dataframe
    # Subjects Gemini silently omitted from its response are marked 'llm_missing'
    triage_counts['predicted_category'] = triage_counts['full_subject'].apply(
        lambda x: llm_predictions.get(x, {}).get('category', 'llm_missing')
    )
    triage_counts['draft_id'] = triage_counts['full_subject'].apply(
        lambda x: llm_predictions.get(x, {}).get('draft_id', '')
    )

    triage_counts.to_csv(triage_output, index=False)

    failure_counts = triage_counts['predicted_category'].value_counts()
    failures = {c: int(failure_counts.get(c, 0)) for c in ('llm_unavailable', 'llm_error', 'llm_missing')}

    # Report statistics
    matched_count = df['id'].notnull().sum()
    total_count = len(df)

    print(f"\n--- Matching Complete ---")
    print(f"Total Rows:       {total_count}")
    print(f"Matched Rows:     {matched_count} ({matched_count/total_count:.1%})")
    print(f"Main Output:      {csv_output}")
    print(f"Triage Output:    {triage_output} ({len(unique_unmatched)} unique unmatched entities to review)")
    if any(failures.values()):
        print(f"\n⚠️  {sum(failures.values())} entities were NOT classified by the LLM:")
        for cat, n in failures.items():
            if n: print(f"      {cat}: {n}")
        print("    These rows have no draft_id. Fix the cause, move the output files, and re-run.")

if __name__ == "__main__":
    paths = parse_volume("Match entities to catmapper IDs and triage the rest with Gemini.")
    client = get_gemini_client()
    process_csv_with_ids(paths)
