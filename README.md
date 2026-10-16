# bahai-index

Converts the indexes of the two volumes of Shahvar et al., *The Baha'is of Iran, Transcaspia and the Caucasus*, into structured data: one row per index entry with its page, the page text, and the location of the mention. The goal is to turn these into entities and annotations in [bahai-data](https://github.com/jacobthill/bahai-data) for use in the GraphRAG ([bahai-rag](https://github.com/jacobthill/bahai-rag)).

## Setup

```bash
uv sync
```

Steps 03 and 04 need a `GEMINI_API_KEY` or `GOOGLE_API_KEY` in `.env`. The Gemini model is set by `GEMINI_MODEL` in `scripts/common.py`.

[bahai-data](https://github.com/jacobthill/bahai-data) must be checked out as a sibling folder (`../bahai-data`); step 4 reads its entity files.

`data/` is not in git. It holds the source PDFs (`data/raw/`) and every generated file. `curated/` is in git and holds hand-edited files (see below).

## Pipeline

Run each step with `uv run python scripts/<script> ...`, from any directory. Steps 02–04 take the volume number (`1` or `2`) as an argument.

1. Run 01_parse_bahais-iran-transcaspia-caucasus-v1.py and 01_parse_bahais-iran-transcaspia-caucasus-v2.py to extract entries and page numbers from the index. --> extracted_csv
   - Run `cross_references.py 1` and `cross_references.py 2` to extract the index's cross-references ("see", "see also", "see under") directly from the PDF, match their headings and targets to entities, and suggest an action for each, e.g. "add 'Shirazi, Sayyid ‘Ali-Muhammad' as a name of person_bab". --> extracted_csv/…-cross-references.csv. Nothing is applied automatically. (Step 1's own cross-reference rows are unreliable and are ignored by step 4.)
2. Run `02_text.py 1` and `02_text.py 2` to get the text for each entry. --> csv_plus_text
   - Headings listed in `curated/corrections.csv` are corrected first. Use it for parsing errors that can't be fixed in step 1's code; never edit generated files.
   - Running-header titles (e.g. "LETTERS OF ALEKSANDR TUMANSKII") are dropped from the page text, so they can't be matched as mentions. Page numbers are kept for the audit. Each volume's header position is `HEADER_BANDS` in `scripts/common.py`.
3. Run `03_index_mentions.py 1` and `03_index_mentions.py 2` to find the mention of the indexed entity in the text. --> csv_final
   - Gemini's answers are cached in `data/cache/step03_llm.jsonl`, so a re-run only calls Gemini for entries whose heading or page text changed. Delete the file to start fresh.
   - Check the output with `audit_pagination.py 1` / `audit_pagination.py 2`. It prints a diagnostic report and exits with code 1 if it finds warnings, so it can gate the next steps: `uv run python scripts/audit_pagination.py 1 && uv run python scripts/review.py build`.
4. Run `review.py build` to update the review sheet, `curated/review.csv`, from both volumes: one row per heading that doesn't match an entity, and per subentry that may be an entity of its own, with likely existing entities, the index's cross-references, and a suggestion. `--batch 50` (the default) also asks Gemini to draft full records, into `curated/drafts.jsonl`, for the 50 rows that most need work. Your verdicts and notes are never changed. See `curated/README.md`.
   - Drafts are based on the book only: first the editors' own entry for the heading (the biographical note, which the index marks with italic page numbers, or the glossary; see `scripts/book_notes.py`), then up to 5 passages from different pages where the name appears. Anything from general knowledge goes only into `draft_notes`, marked unverified. Each description records its source (`description.source`, e.g. "…, vol. 1, pp. 198–199 (biographical note)"). If the sources aren't enough, the description is left empty and the draft is marked `needs_research`. Birth and death years printed in a biographical note are filled in from it, and any year not found in a draft's sources is flagged with `CHECK:` in its `draft_notes`.
5. Run `review_page.py <batch>` for a readable page of the batch (`data/review/batch-<n>.html`): each row's decision and draft, next to the passages it was based on. Then review the batch in `curated/review.csv` (fill in `verdict`) and `curated/drafts.jsonl` (check the drafts, especially `draft_notes`).
6. Run `04_match_entities.py 1` and `04_match_entities.py 2` to match each entry to an entity. --> bahais-of-iran-transcaspia-caucasus-v?-text-with-ids.csv. An entry is matched on its full text first (heading and subentry, which can name a specific event), then on its heading alone; `id_matched_on` records which.

Shared paths, file names, and the Gemini model (`GEMINI_MODEL`) are set in `scripts/common.py`.

## Entity matching

An entity exists only if it is in bahai-data. Matching uses the names and variant labels of the bahai-data authority files (people, places, events, groups, subjects, circular instructions), read directly from `../bahai-data/data` on every run, plus your decisions in `curated/review.csv`: an approved link adds the row's text as a name of an existing entity, and an approved new entity (with its draft in `curated/drafts.jsonl`) is matched as `pending` until it is added to bahai-data.

Each row of `-text-with-ids.csv` gets an `id` and an `id_status`: `exists` or `pending`. Names shared by two entities are reported and not matched, so they go to review instead of being guessed. Works are not matched by title, since titles such as "Baghdad" cause false matches; works are matched only through approved review rows. Matching code is in `scripts/entities.py`.

## Known issues

- Approved decisions are not yet written to bahai-data; that is the next step (an apply script that writes them to a new bahai-data branch for review).

## Direction

Agreed but not yet implemented:

- An apply script that writes approved review rows and drafts to bahai-data on a new branch, and marks them `applied`.
- Export one manifestation per volume (text in `bahai-data/data/copyright/`) and annotations with a `page` field, which bahai-rag uses to cite by book and page. An entry matched on a subentry entity (e.g. an event) should also be annotated with its heading's entity.
