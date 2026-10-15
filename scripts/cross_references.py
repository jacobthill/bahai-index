"""Extracts the index's cross-references ("see", "see also", "see under") directly from the PDF.

Reads the index pages line by line instead of relying on step 01's parser, which loses or
misattributes many cross-references. For each one it records the heading it belongs to, the
kind of reference, and each target (a reference can list several, separated by ';').

  "Persia see Iran"                  -> see:        Persia -> Iran (two names for one thing)
  "see also tujjar" under merchants  -> see_also:   merchants -> tujjar (related entries)
  "Porte, Sublime see under Ottoman Empire" -> see_under: the topic is a subentry of the target

Each heading and target is matched against bahai-data (and the curated pending files), and a
suggested action is added for the review sheet; nothing is changed automatically.

Usage: uv run python scripts/cross_references.py <volume>
"""
import re
import fitz
import pandas as pd
from common import parse_volume
from entities import build_lookup, resolve

NOISE = ["INDEX", "numbers denote", "italic numbers"]
PAGE_TOKEN = re.compile(r'^(\d+(n\d+)?([–—-]\d+)?|[ivxlc]+)$')
SEE = re.compile(r'\bsee\b')

def split_text_and_pages(text):
    """Returns (text before the first page number, whether the line has page numbers)."""
    for m in re.finditer(r'[^\s,;]+', text):
        if PAGE_TOKEN.match(m.group().strip('.')):
            return text[:m.start()].strip(" ,;"), True
    return text.strip(" ,;"), False

def index_lines(doc, pages, header_band):
    """Yields (page, is_baseline, text) for each index line, column by column, top to bottom."""
    _, bottom = header_band
    for page_num in pages:
        if page_num > len(doc):
            continue  # the configured range runs past the end of the PDF
        page = doc.load_page(page_num - 1)
        width = page.rect.width
        lines = []
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                text = "".join(s["text"] for s in l["spans"]).strip()
                y0, y1 = l["bbox"][1], l["bbox"][3]
                if not text or y0 < bottom or any(n in text for n in NOISE):
                    continue
                lines.append((l["bbox"][0], y0, text))
        for column in ([l for l in lines if l[0] < width / 2], [l for l in lines if l[0] >= width / 2]):
            text_xs = [x for x, _, t in column if not re.match(r'^\d', t)]
            if not text_xs:
                continue
            baseline = min(text_xs)
            for x, _, text in sorted(column, key=lambda l: l[1]):
                yield page_num, abs(x - baseline) < 7, text

def parse_targets(reference):
    """'also under Bab, the; Imam, Twelfth' -> ('see_under', ['Bab, the', 'Imam, Twelfth'])."""
    ref = reference.strip(" ,.;")
    kind = 'see'
    if re.match(r'(?i)^also\b', ref):
        kind, ref = 'see_also', re.sub(r'(?i)^also\s*', '', ref)
    if re.match(r'(?i)^under\b', ref):
        kind, ref = 'see_under', re.sub(r'(?i)^under\s*', '', ref)
    targets = [t.strip(" ,.;") for t in ref.split(';') if t.strip(" ,.;")]
    return kind, targets

def extract_cross_references(paths):
    doc = fitz.open(paths.pdf)
    lines = list(index_lines(doc, paths.index_pages, paths.header_band))
    rows = []
    heading, heading_open, i = None, False, 0
    while i < len(lines):
        page, is_baseline, text = lines[i]
        parts = SEE.split(text, maxsplit=1)
        has_see = len(parts) == 2
        before_see, after_see = parts[0], (parts[1] if has_see else "")
        label, has_pages = split_text_and_pages(before_see)

        if is_baseline and label:
            heading = label
            source = heading
            heading_open = not has_pages and not has_see
        elif heading_open and label:
            # Continuation of a heading that wrapped, e.g. "... Imperator-" / "skogo ... obshchestva"
            heading = f"{heading[:-1]}{label}" if heading.endswith("-") else f"{heading} {label}"
            source = heading
            heading_open = not has_pages and not has_see
        elif label and (has_pages or has_see):
            source = f"{heading}: {label}"   # a subentry, e.g. "protection of see under Russia"
        else:
            source = heading                 # "see also X" on its own line belongs to the heading

        if not is_baseline and not heading_open and (has_pages or has_see):
            heading_open = False
        if has_see:
            reference = after_see.strip()
            # Join continuation lines: indented, no page numbers, no new 'see'
            while i + 1 < len(lines):
                _, next_baseline, next_text = lines[i + 1]
                if next_baseline or SEE.search(next_text) or split_text_and_pages(next_text)[1]:
                    break
                joiner = "" if reference.endswith("-") else " "
                reference = f"{reference}{joiner}{next_text.strip()}".strip()
                i += 1
            kind, targets = parse_targets(reference)
            for target in targets:
                rows.append({"page": page, "heading": source, "kind": kind, "target": target})
        i += 1
    return pd.DataFrame(rows, columns=["page", "heading", "kind", "target"])

def suggest(kind, heading, target, heading_id, target_id):
    """Suggested action for one cross-reference, for review."""
    heading_id = heading_id if pd.notna(heading_id) else None
    target_id = target_id if pd.notna(target_id) else None
    if kind == 'see':
        if heading_id and target_id:
            return "both match the same entity" if heading_id == target_id else \
                   f"possible duplicate: {heading_id} and {target_id} name the same thing"
        if target_id:
            return f"add '{heading}' as a name of {target_id}"
        if heading_id:
            return f"add '{target}' as a name of {heading_id}"
        return "neither matches: one entity with two names"
    if kind == 'see_also':
        if heading_id and target_id and heading_id == target_id:
            return "both match the same entity"
        if heading_id and target_id:
            return f"related: {heading_id} and {target_id}"
        return "related entries (not both matched yet)"
    return f"'{heading}' is covered under the heading '{target}'"

def match_heading(name, lookup):
    """Matches a heading or target, trying the main heading if it has a subentry ('Iran: government of')."""
    uid, _ = resolve(name, lookup)
    if not uid and ": " in name:
        uid, _ = resolve(name.split(": ", 1)[0], lookup)
    return uid

if __name__ == "__main__":
    paths = parse_volume("Extract the index's cross-references from the PDF.")
    df = extract_cross_references(paths)
    lookup = build_lookup()
    df["heading_id"] = df["heading"].map(lambda h: match_heading(h, lookup))
    df["target_id"] = df["target"].map(lambda t: match_heading(t, lookup))
    df["suggestion"] = [suggest(r.kind, r.heading, r.target, r.heading_id, r.target_id) for r in df.itertuples()]
    pd.set_option("display.max_colwidth", 60); pd.set_option("display.width", 250)
    paths.cross_refs_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.cross_refs_csv, index=False)
    print(df.to_string(index=False))
    print(f"\n{len(df)} cross-references saved to {paths.cross_refs_csv}")
