"""The editors' own entries in each volume: biographical notes and glossary terms.

These are the most reliable source for an entity's description, so drafting uses them first.
Entries are read from the PDF pages in NOTE_SECTIONS (common.py). Each entry starts a new text
block: its heading, then (for a person) dates in parentheses, or (for a term) a language tag.

    find_note(heading, vol) -> {"kind", "heading", "page", "text"} or None
"""
import re
import fitz
from common import volume_paths
from entities import normalize_for_match, without_parenthetical

TITLES = {"general", "baron", "count", "prince", "princess", "sir", "archpriest", "comte", "de", "von"}
LANG_TAG = re.compile(r'\((?:from )?(?:Pers|Ar|Arab|Turk|Russ|Ital|Fr|Gr|Lat)\.\)\s*$')
_cache = {}

def _printed_page(page, header_band):
    """The page number printed in the running header, or None."""
    top, bottom = header_band
    for b in page.get_text("blocks"):
        if top <= b[1] and b[3] <= bottom:
            m = re.search(r'(?<![\w-])(\d{1,3})(?![\w-])', b[4])
            if m:
                return m.group(1)
    return None

DATES = re.compile(r'^\((?:d\.|b\.|c\.|fl\.)?\s*\d')
INLINE_DATES = re.compile(r'^(.+?)\s+\((?:d\.|b\.|c\.|fl\.)?\s*\d.*$')

PARTICLES = {"al", "bin", "ibn", "de", "von", "of", "the", "and", "la", "le", "du", "y"}

def _looks_like_name(line):
    """A heading line: mostly capitalized words, no sentence inside, no final full stop."""
    if not line or line.endswith('.') or '. ' in line or re.search(r'\d', line):
        return False
    words = [w for w in re.split(r'[\s,]+', line) if re.search(r'[A-Za-z]', w)]
    if not words or len(line) > 80:
        return False
    caps = sum(1 for w in words if w.lstrip("‘'(").lower().split("-")[0] in PARTICLES or w.lstrip("‘'(")[:1].isupper())
    return caps / len(words) >= 0.75

def _entry_starts(kind, lines):
    """Indexes of the lines that begin an entry. A glossary entry is a term line ending in a
    language tag ('Pishkish (Pers.)'). A biographical note begins on a name-like line right after
    the previous entry's last sentence, followed by its dates ('(1846–1879), ...') or, for a
    'Surname, Given' name, directly by the description."""
    starts = []
    for i, line in enumerate(lines):
        if kind == "glossary":
            if LANG_TAG.search(line) and (line[:1].isupper() or line.startswith(("‘", "'"))):
                starts.append(i)
            continue
        after_sentence = i == 0 or re.search(r'[.)’”"]$', lines[i - 1])
        if not after_sentence:
            continue
        inline = INLINE_DATES.match(line)        # 'Nasir al-Din Shah Qajar (1831–1896), Iranian king'
        if inline and _looks_like_name(inline.group(1)):
            starts.append(i)
            continue
        if not _looks_like_name(line):
            continue
        dated = any(DATES.match(l) for l in lines[i + 1:i + 3])
        described = i + 1 < len(lines) and lines[i + 1][:1].islower() and len(line.split()) <= 6
        if dated or ',' in line or described:
            starts.append(i)
    return starts

def _heading_of(kind, chunk_lines):
    """The entry's name: the line(s) before the dates, or just the first line when the
    description follows directly (undated notes and glossary terms)."""
    first = chunk_lines[0]
    inline = INLINE_DATES.match(first)
    if inline:
        return inline.group(1)
    if kind == "glossary" or not any(DATES.match(l) for l in chunk_lines[1:3]):
        return first
    head = []
    for l in chunk_lines:
        if DATES.match(l):
            break
        head.append(l)
    return re.sub(r'-\s+(?=[a-z])', '', " ".join(head))

def load_notes(vol):
    """All entries of a volume: [{"kind", "heading", "page", "pages", "text"}]."""
    if vol in _cache:
        return _cache[vol]
    paths = volume_paths(vol)
    doc = fitz.open(paths.pdf)
    _, bottom = paths.header_band
    entries = []
    for kind, pages in paths.note_sections.items():
        # Every line of the section in reading order, with the page it is printed on
        lines, printed_pages = [], []
        for page_num in pages:
            page = doc.load_page(page_num - 1)
            printed_pages.append(_printed_page(page, paths.header_band))
            for b in sorted(page.get_text("blocks"), key=lambda b: (b[0] > page.rect.width / 2, b[1])):
                if b[1] < bottom:
                    continue
                for l in b[4].strip().split("\n"):
                    if l.strip() and l.strip() not in ("GLOSSARY", "BIOGRAPHICAL NOTES"):
                        lines.append((len(printed_pages) - 1, l.strip()))
        # A section's first page has no running header: number it from the next page
        for i in range(len(printed_pages) - 2, -1, -1):
            if printed_pages[i] is None and printed_pages[i + 1] and printed_pages[i + 1].isdigit():
                printed_pages[i] = str(int(printed_pages[i + 1]) - 1)
        texts = [t for _, t in lines]
        starts = _entry_starts(kind, texts)
        for n, start in enumerate(starts):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines)
            chunk = lines[start:end]
            text = re.sub(r'(\w)-\s+([a-z])', r'\1\2', " ".join(t for _, t in chunk))
            pages_of = list(dict.fromkeys(printed_pages[p] for p, _ in chunk if printed_pages[p]))
            entries.append({"kind": kind, "heading": _heading_of(kind, [t for _, t in chunk]),
                            "pages": pages_of, "text": text,
                            "page": pages_of[0] if len(pages_of) == 1 else f"{pages_of[0]}–{pages_of[-1]}"})
    _cache[vol] = entries
    return entries

def _name_parts(name):
    """(surname, other words) for matching 'Kuropatkin, General Aleksei Nikolaevich' with
    'Kuropatkin, Aleksei Nikolaevich'. Titles and parentheticals are ignored."""
    name = without_parenthetical(LANG_TAG.sub('', name))
    surname, _, rest = name.partition(',')
    rest_words = {w for w in normalize_for_match(rest).split() if w not in TITLES}
    return normalize_for_match(surname), rest_words

def _similarity(a, b):
    """Share of the shorter name's words found in the other: 'azal mirza yahya' vs
    'azal mirza nuri yahya' -> 1.0. Names are compared as word sets, so order doesn't matter."""
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0

def _plain(name):
    """Normalized name words, without titles or language tags (parentheticals kept, since they
    often hold the other name: 'Azal (Mirza Yahya Nuri)')."""
    words = normalize_for_match(LANG_TAG.sub('', name)).split()
    return " ".join(w for w in words if w not in TITLES)

def find_note(heading, vol, note_pages=(), kind=None):
    """The editors' entry for an index heading, or None.

    With note_pages (the index's biographical-note page references for the heading), the entry
    is chosen among those printed on those pages, by name similarity. Without, a glossary term
    must match exactly, and a person must share surname and at least one other name."""
    entries = [e for e in load_notes(vol) if kind is None or e["kind"] == kind]
    target = _plain(heading)
    wanted = {str(p).split("–")[0].rstrip("n") for p in note_pages}
    if wanted:
        on_pages = [e for e in entries if wanted & set(e["pages"]) and e["kind"] == "biographical note"]
        scored = sorted(((_similarity(target, _plain(e["heading"])), e) for e in on_pages), key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.6:
            return scored[0][1]
    surname, rest = _name_parts(heading)
    for e in entries:
        e_surname, e_rest = _name_parts(e["heading"])
        if e["kind"] == "glossary" and normalize_for_match(LANG_TAG.sub('', e["heading"])) == normalize_for_match(heading):
            return e
        if e["kind"] == "biographical note" and e_surname == surname and rest and (rest & e_rest):
            return e
    return None
