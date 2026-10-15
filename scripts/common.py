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

# Hand-edited, tracked in git: entities and labels approved but not yet in bahai-data
CURATED_DIR = PROJECT_ROOT / "curated"
NEW_ENTITIES_JSONL = CURATED_DIR / "new_entities.jsonl"
NEW_LABELS_CSV = CURATED_DIR / "new_labels.csv"
CORRECTIONS_CSV = CURATED_DIR / "corrections.csv"

VOLUMES = ("1", "2")
BASE_NAME = "bahais-of-iran-transcaspia-caucasus-v{vol}"

# Pinned for reproducibility; changing it may change LLM-derived results
GEMINI_MODEL = "gemini-3.8-flash"

def volume_paths(vol):
    """Returns every input/output path for one volume of the pipeline."""
    name = BASE_NAME.format(vol=vol)
    return SimpleNamespace(
        vol=vol,
        pdf=DATA_DIR / "raw" / f"{name}.pdf",
        extracted_csv=DATA_DIR / "extracted_csv" / f"{name}.csv",           # step 01
        context_csv=DATA_DIR / "csv_plus_text" / f"{name}-text-context.csv",  # step 02
        final_csv=DATA_DIR / "csv_final" / f"{name}-text-final.csv",          # step 03
        with_ids_csv=DATA_DIR / "csv_final" / f"{name}-text-with-ids.csv",    # step 04
        triage_csv=DATA_DIR / "csv_final" / f"triage_review_v{vol}.csv",      # step 04
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

def generate_json(client, prompt):
    """Sends a prompt to Gemini and parses the JSON response. Raises on any failure."""
    from google.genai import types

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)
