# bahai-index

Converts the indexes of the two volumes of Shahvar et al., *The Baha'is of Iran, Transcaspia and the Caucasus*, into structured data: one row per index entry with its page, the page text, and the location of the mention. The goal is to turn these into entities and annotations in [bahai-data](https://github.com/jacobthill/bahai-data) for use in the GraphRAG ([bahai-rag](https://github.com/jacobthill/bahai-rag)).

## Setup

```bash
uv sync
```

Steps 03 and 04 need a `GEMINI_API_KEY` or `GOOGLE_API_KEY` in `.env`. The Gemini model is set by `GEMINI_MODEL` in `scripts/common.py`.

`data/` is not in git. It holds the source PDFs (`data/raw/`), every generated file, and `data/catmapper.yaml` (see below).

## Pipeline

Run each step with `uv run python scripts/<script> ...`, from any directory. Steps 02–04 take the volume number (`1` or `2`) as an argument.

1. Run 01_parse_bahais-iran-transcaspia-caucasus-v1.py and 01_parse_bahais-iran-transcaspia-caucasus-v2.py to extract entries and page numbers from the index. --> extracted_csv
2. Run `02_text.py 1` and `02_text.py 2` to get the text for each entry. --> csv_plus_text
3. Run `03_index_mentions.py 1` and `03_index_mentions.py 2` to find the mention of the indexed entity in the text. --> csv_final
   - Check the output with `audit_pagination.py 1` / `audit_pagination.py 2`. It prints a diagnostic report and exits with code 1 if it finds warnings, so it can gate step 4: `uv run python scripts/audit_pagination.py 1 && uv run python scripts/04_triage_entities.py 1`.
4. Run `04_triage_entities.py 1` and `04_triage_entities.py 2` to assign a preliminary category to each entity and try to match its id. --> bahais-of-iran-transcaspia-caucasus-v?-text-with-ids.csv, triage_review_v?.csv. Refuses to run if those files already exist; move them first.
5. Manually review all ids and categories in triage_review_v?.csv. Save the reviewed copy under a different name so a re-run of step 4 can't be confused with it.

Shared paths, file names, and the Gemini model (`GEMINI_MODEL`) are set in `scripts/common.py`.

## Entity matching and `catmapper.yaml`

Step 4 matches index headings against `data/catmapper.yaml`, a flat list of entity IDs and names. It is generated from the bahai-data authority files by `scripts/build_catmapper.py` in bahai-data. The copy here has diverged: it contains 29 entities added by hand during review that are not in bahai-data, and lacks a few added to bahai-data since.

## Known issues

- Manual review (step 5) is not read by any script, so reviewed decisions do not reach bahai-data or later runs.
- Step 4 matches on the full heading including the subentry (e.g. "Tumanskii, Aleksandr Grigor'evich: and Kitabi Aqdas"), which lowers the match rate.
- Step 1 sometimes joins two subentries into one; 17 of these were corrected by hand in the v1 `-text-with-ids.csv`, which a re-run would lose.
- The audit fails on both volumes: a few mentions match the page's running header instead of the body text.

## Direction

Agreed but not yet implemented:

- Read entities from bahai-data directly instead of a `catmapper.yaml` copy, after moving the 29 hand-added entities into bahai-data.
- Keep hand corrections to parsed headings in a corrections file tracked in git, applied after step 1. Never edit generated files.
- Replace the triage file with a review sheet of proposed actions (link to an existing entity, create one, ignore), and an apply script that writes approved changes to bahai-data on a new branch.
- Model subentries such as "Bab, the: execution of" as events, like existing bahai-data events.
- Export one manifestation per volume (text in `bahai-data/data/copyright/`) and annotations with a `page` field, which bahai-rag uses to cite by book and page.
