"""Shared paths, per-volume file names, and Gemini setup for the pipeline scripts."""
import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

# Paths are resolved from this file's location, so scripts work from any directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

# bahai-data is the only definition of which entities exist; read from a sibling checkout
BAHAI_DATA_DIR = PROJECT_ROOT.parent / "bahai-data" / "data"

# Hand-edited, tracked in git: review decisions and draft records not yet in bahai-data
CURATED_DIR = PROJECT_ROOT / "curated"
REVIEW_CSV = CURATED_DIR / "review.csv"
DRAFTS_JSONL = CURATED_DIR / "drafts.jsonl"
CORRECTIONS_CSV = CURATED_DIR / "corrections.csv"

VOLUMES = ("1", "2")
BASE_NAME = "bahais-of-iran-transcaspia-caucasus-v{vol}"

# Vertical band (PDF points from the top) holding each volume's running header and page number.
# Checked against every page: nothing else is printed there.
HEADER_BANDS = {"1": (30, 50), "2": (60, 80)}

# Pages of each PDF (1-based) holding the editors' biographical notes and glossary
NOTE_SECTIONS = {
    "1": {"biographical note": range(197, 217), "glossary": range(217, 220)},
    "2": {"biographical note": range(167, 188), "glossary": range(189, 193)},
}

# Pages of each PDF (1-based) that hold the index
INDEX_PAGES = {"1": range(290, 310), "2": range(229, 337)}

# Pinned for reproducibility; changing it may change LLM-derived results
GEMINI_MODEL = "gemini-3.8-flash"

def volume_paths(vol):
    """Returns every input/output path for one volume of the pipeline."""
    name = BASE_NAME.format(vol=vol)
    return SimpleNamespace(
        vol=vol,
        header_band=HEADER_BANDS[vol],
        index_pages=list(INDEX_PAGES[vol]),
        note_sections=NOTE_SECTIONS[vol],
        pdf=DATA_DIR / "raw" / f"{name}.pdf",
        extracted_csv=DATA_DIR / "extracted_csv" / f"{name}.csv",           # step 01
        cross_refs_csv=DATA_DIR / "extracted_csv" / f"{name}-cross-references.csv",  # cross_references.py
        context_csv=DATA_DIR / "csv_plus_text" / f"{name}-text-context.csv",  # step 02
        final_csv=DATA_DIR / "csv_final" / f"{name}-text-final.csv",          # step 03
        with_ids_csv=DATA_DIR / "csv_final" / f"{name}-text-with-ids.csv",    # step 04
    )

def apply_corrections(df, vol):
    """Applies curated/corrections.csv to step 01 output: rows whose full_subject equals
    parsed_heading get corrected_heading ("Main: Sub" or "Main"). Warns about corrections
    that match nothing, so stale ones are noticed."""
    import pandas as pd
    if not CORRECTIONS_CSV.exists():
        return df
    corrections = pd.read_csv(CORRECTIONS_CSV, dtype=str).fillna("")
    corrections = corrections[corrections["volume"] == str(vol)]
    for _, c in corrections.iterrows():
        rows = df["full_subject"] == c["parsed_heading"]
        if not rows.any():
            print(f"  [!] Correction matches no heading in v{vol}: '{c['parsed_heading']}'")
            continue
        main, _, sub = c["corrected_heading"].partition(": ")
        df.loc[rows, ["main_subject", "sub_subject", "full_subject"]] = [main, sub, c["corrected_heading"]]
        print(f"  Corrected {rows.sum()} rows: '{c['parsed_heading']}' -> '{c['corrected_heading']}'")
    return df

def parse_volume(description):
    """Parses the required volume argument and returns that volume's paths."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("volume", choices=VOLUMES, help="Volume number")
    return volume_paths(parser.parse_args().volume)

def get_gemini_client():
    """Returns a Gemini client, or None (with a warning) if no API key is set."""
    from dotenv import load_dotenv
    from google import genai

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("WARNING: No Gemini API Key found. LLM calls will be marked as failures.")
        return None
    return genai.Client(api_key=api_key)

_json_caches = {}

def cached_generate_json(client, prompt, cache_path):
    """generate_json with a JSONL cache keyed by model + prompt, so re-runs only call Gemini
    for prompts not seen before. Only successful answers are cached. Raises on failure."""
    import hashlib
    if cache_path not in _json_caches:
        cache = {}
        if cache_path.exists():
            with open(cache_path, encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        entry = json.loads(line)
                        cache[entry['key']] = entry['response']
        _json_caches[cache_path] = cache
    cache = _json_caches[cache_path]
    key = hashlib.sha256(f"{GEMINI_MODEL}\n{prompt}".encode('utf-8')).hexdigest()
    if key in cache:
        return cache[key]
    if not client:
        raise RuntimeError("No Gemini API key configured")
    response = generate_json(client, prompt)
    cache[key] = response
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'key': key, 'response': response}, ensure_ascii=False) + "\n")
    return response

def generate_json(client, prompt):
    """Sends a prompt to Gemini and parses the JSON response. Raises on any failure."""
    from google.genai import types

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)
