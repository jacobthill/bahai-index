# curated/

Files edited by hand. Unlike `data/`, this folder is tracked in git, and no pipeline script overwrites it.

- `new_entities.jsonl`: new entities approved but not yet in bahai-data, one record per line in bahai-data's format. `"status": "draft"` marks records whose fields are not yet complete. Once a record is added to bahai-data, remove it here; step 4 warns about any that are already there.
- `new_labels.csv`: extra names for entities that exist in bahai-data (`id,label,lang,note`), for example the index's spelling of a name.
- `corrections.csv`: fixes to headings as parsed by step 01 (`volume,parsed_heading,corrected_heading,note`), applied at the start of step 02. `corrected_heading` is `Main: Sub` or just `Main`. Step 02 warns about corrections that no longer match anything.

Planned:

- `review.csv`: the review sheet. Step 04 adds rows for new unmatched headings; the verdict column is yours.
