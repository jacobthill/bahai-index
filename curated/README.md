# curated/

Files edited by hand. Unlike `data/`, this folder is tracked in git, and no pipeline script overwrites your edits.

## review.csv: the review sheet

One row per index heading that doesn't match an entity, and per subentry that may be an entity of its own (e.g. "Nasir al-Din Shah Qajar: assassination of"). Built and updated by `uv run python scripts/review.py build`. Open it in Numbers or any spreadsheet app and save it back as CSV.

| Column | Who | Meaning |
|---|---|---|
| `kind` | script | `heading` or `subentry` |
| `text` | script | the heading (or "Heading: subentry") as printed in the index |
| `volumes`, `mentions`, `subentries` | script | where it appears, how often, and its most frequent subentries |
| `candidates` | script | up to 3 likely existing entities: `id (name, similarity)` |
| `cross_refs` | script | the index's own cross-references, e.g. `index v1: see Iran` |
| `suggestion` | script | `link:<id>` (an existing entity), `new:<id>` (the prefix gives the type), or `ignore`. Never changed once set. |
| **`verdict`** | **you** | blank (undecided), `approve` (accept the suggestion), `link:<id>`, `new:<id>`, or `ignore` |
| **`notes`** | **you** | anything |
| `status` | script | `proposed`, `approved`, `ignored`, or `applied` (added to bahai-data) |
| `batch` | script | the batch the row was drafted in |

An approved `link` makes the row's text a name of that entity at once. An approved `new` entity counts as pending until it is added to bahai-data.

## drafts.jsonl: draft records

One record per line, in bahai-data's format, for new entities. `review.py build --batch N` asks Gemini to draft the N rows that most need work, using the index passages. Extra fields, to be removed when a record is added to bahai-data:

- `"status": "draft"`
- `draft_sources`: the volume and page of every mention
- `draft_notes`: what Gemini was unsure of, or took from general knowledge rather than the passages (check these)

Edit drafts freely: a draft is never overwritten, and a rebuild only fills its empty fields.

## corrections.csv

Fixes to headings as parsed by step 01 (`volume,parsed_heading,corrected_heading,note`), applied at the start of step 02. `corrected_heading` is `Main: Sub` or just `Main`. Step 02 warns about corrections that no longer match anything.
