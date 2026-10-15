import pandas as pd
import sys
import json
import time
from common import parse_volume, get_gemini_client, generate_json
from entities import build_lookup, resolve

client = None  # Set in __main__ via get_gemini_client()

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
    csv_input = paths.final_csv
    if not csv_input.exists():
        print(f"[!] Error: Could not find {csv_input}. Did you run step 03 first?")
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

    print("Building lookup from bahai-data and curated files...")
    lookup = build_lookup()

    print(f"Reading CSV: {csv_input.name}...")
    df = pd.read_csv(csv_input)

    print("Resolving IDs...")
    resolved = df['full_subject'].apply(lambda s: resolve(s, lookup) if pd.notna(s) else (None, None))
    df['id'] = resolved.str[0]
    # 'exists': in bahai-data; 'pending': only in curated/new_entities.jsonl, not yet applied
    df['id_status'] = resolved.str[1]

    # Reorder columns to put 'id' and 'id_status' first
    cols = [c for c in df.columns if c not in ('id', 'id_status')]
    df = df[['id', 'id_status'] + cols]

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
    total_count = len(df)
    exists_count = (df['id_status'] == 'exists').sum()
    pending_count = (df['id_status'] == 'pending').sum()

    print(f"\n--- Matching Complete ---")
    print(f"Total Rows:       {total_count}")
    print(f"Matched Rows:     {exists_count + pending_count} ({(exists_count + pending_count)/total_count:.1%})")
    print(f"  in bahai-data:  {exists_count}")
    print(f"  pending:        {pending_count} (entity in curated/new_entities.jsonl, not yet in bahai-data)")
    print(f"Main Output:      {csv_output}")
    print(f"Triage Output:    {triage_output} ({len(unique_unmatched)} unique unmatched entities to review)")
    if any(failures.values()):
        print(f"\n⚠️  {sum(failures.values())} entities were NOT classified by the LLM:")
        for cat, n in failures.items():
            if n: print(f"      {cat}: {n}")
        print("    These rows have no draft_id. Fix the cause, move the output files, and re-run.")

if __name__ == "__main__":
    paths = parse_volume("Match headings to bahai-data entities and triage the rest with Gemini.")
    client = get_gemini_client()
    process_csv_with_ids(paths)
