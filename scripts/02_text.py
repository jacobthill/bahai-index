import fitz
import pandas as pd
import re
from common import parse_volume

def is_roman_numeral(s):
    s = str(s).strip(" \t\n\r,.;()[]").lower()
    if not s: return False
    return bool(re.match(r'^(l?x{0,3})(ix|iv|v?i{0,3})$', s))

def roman_to_int(s):
    s = str(s).strip(" \t\n\r,.;()[]").lower()
    rom_val = {'i': 1, 'v': 5, 'x': 10, 'l': 50, 'c': 100}
    int_val = 0
    for i in range(len(s)):
        if i > 0 and rom_val.get(s[i], 0) > rom_val.get(s[i - 1], 0):
            int_val += rom_val.get(s[i], 0) - 2 * rom_val.get(s[i - 1], 0)
        else:
            int_val += rom_val.get(s[i], 0)
    return int_val

def get_base_page(page_str):
    if pd.isna(page_str): return None, None
    page_str = str(page_str).strip().lower()

    match_arabic = re.search(r'^(\d+)', page_str)
    if match_arabic:
        return 'arabic', int(match_arabic.group(1))

    match_roman = re.search(r'^([xivlc]+)', page_str)
    if match_roman and is_roman_numeral(match_roman.group(1)):
        return 'roman', roman_to_int(match_roman.group(1))

    return None, None

def build_page_map(doc):
    arabic_map = {}
    roman_map = {}

    for i in range(len(doc)):
        page = doc[i]

        # --- POISON DEFENSE 1: Keyword Flags ---
        top_text = page.get_text("text")[:300].upper()
        is_poison_page = any(kw in top_text for kw in [
            'ILLUSTRATION', 'CONTENTS', 'INDEX', 'BIBLIOGRAPHY', 'PREFACE', 'INTRODUCTION'
        ])

        blocks = page.get_text("blocks")
        rect = page.rect

        blocks.sort(key=lambda b: b[1])
        if not blocks: continue

        edge_blocks = blocks[:3] + blocks[-3:]

        for b in edge_blocks:
            if b[1] > rect.height * 0.15 and b[3] < rect.height * 0.85:
                continue

            block_text = b[4].strip()
            if not block_text: continue

            # Length & Format Caps
            if len(block_text) > 80 or block_text.count('.') > 3:
                continue

            if '\n' in block_text:
                lines = [l.strip() for l in block_text.split('\n') if l.strip()]
                if len(lines) == 2:
                    if lines[0].isdigit() and not is_poison_page:
                        val = int(lines[0])
                        if 0 < val < 1000 and val not in arabic_map: arabic_map[val] = i
                    elif lines[1].isdigit() and not is_poison_page:
                        val = int(lines[1])
                        if 0 < val < 1000 and val not in arabic_map: arabic_map[val] = i
                continue

            if block_text.isdigit():
                val = int(block_text)
                if 0 < val < 1000 and not is_poison_page and val not in arabic_map:
                    arabic_map[val] = i
            elif is_roman_numeral(block_text):
                val = roman_to_int(block_text)
                if 0 < val < 100 and val not in roman_map:
                    roman_map[val] = i
            else:
                words = block_text.split()
                if words:
                    if words[0].isdigit() and not is_poison_page:
                        val = int(words[0])
                        if 0 < val < 1000 and val not in arabic_map: arabic_map[val] = i
                    elif words[-1].isdigit() and not is_poison_page:
                        val = int(words[-1])
                        if 0 < val < 1000 and val not in arabic_map: arabic_map[val] = i
                    elif is_roman_numeral(words[0]):
                        val = roman_to_int(words[0])
                        if 0 < val < 100 and val not in roman_map: roman_map[val] = i
                    elif is_roman_numeral(words[-1]):
                        val = roman_to_int(words[-1])
                        if 0 < val < 100 and val not in roman_map: roman_map[val] = i

    # --- POISON DEFENSE 2: Front-Matter Isolation ---
    # Find the physical boundary where the Roman numeral front-matter ends.
    front_matter_physical_pages = [p for p in roman_map.values() if p < 50]
    front_matter_end = max(front_matter_physical_pages) if front_matter_physical_pages else 25

    clean_arabic_map = {}
    for logical, physical in arabic_map.items():
        # If an Arabic page was mapped to a physical page inside the front-matter,
        # AND its logical number is higher than the physical page index (e.g. logical page 27 on physical page 11)
        # it is a false positive from a Table of Contents. Delete it.
        if physical <= front_matter_end and logical > physical:
            continue
        clean_arabic_map[logical] = physical

    return clean_arabic_map, roman_map

def estimate_physical_page(target_type, target_logical, arabic_map, roman_map, max_pages):
    page_map = arabic_map if target_type == 'arabic' else roman_map

    if target_logical in page_map:
        return page_map[target_logical]

    known_logicals = sorted(page_map.keys())
    if not known_logicals:
        return None

    closest_logical = min(known_logicals, key=lambda k: abs(k - target_logical))
    physical_offset = target_logical - closest_logical
    estimated_physical = page_map[closest_logical] + physical_offset

    if 0 <= estimated_physical < max_pages:
        return estimated_physical
    return None

def is_likely_caption(text):
    text_strip = text.strip()
    if re.match(r'^(Figure\s+\d+|\d+\.)\s+[A-Z]', text_strip, re.IGNORECASE):
        return True
    if "Courtesy" in text_strip or "©" in text_strip or "World Centre" in text_strip:
        return True
    return False

def clean_page_content(text):
    text = re.sub(r'Vol\.?\s*\d+.*?\d{1,2}/\d{1,2}/\d{2,4}.*?Page\s+\d+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^\s*\d{4}\s*[^\w\s]\s*\d{4}.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'THE BAHA[’\']IS OF IRAN.*$', '', text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)

    lines = text.split('\n')
    healed_text = ""
    for i in range(len(lines)):
        line = lines[i].strip()
        if not line:
            healed_text += "\n\n"
            continue
        healed_text += " " + line
        if line.endswith(('.', '!', '?', ':', '"', '’')):
            if i + 1 < len(lines) and lines[i+1].strip() and not lines[i+1].strip()[0].islower():
                healed_text += "\n\n"

    text = re.sub(r'[ \t]+', ' ', healed_text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()

def get_clean_page_text(doc, page_index):
    if page_index < 0 or page_index >= len(doc): return ""
    page = doc.load_page(page_index)
    blocks = page.get_text("blocks")
    blocks.sort(key=lambda b: (b[1], b[0]))
    valid_blocks = [b[4] for b in blocks if not is_likely_caption(b[4])]
    return clean_page_content("\n".join(valid_blocks))

def get_substantial_page_text(doc, start_idx, direction, min_chars=400):
    current_idx = start_idx
    while 0 <= current_idx < len(doc):
        text = get_clean_page_text(doc, current_idx)
        if len(text) >= min_chars and "\n\n" in text:
            return text
        current_idx += direction
    return ""

def process_corpus(paths):
    paths.context_csv.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(paths.pdf)
    df = pd.read_csv(paths.extracted_csv)

    arabic_map, roman_map = build_page_map(doc)
    max_pages = len(doc)
    pre_list, tgt_list, post_list = [], [], []

    for _, row in df.iterrows():
        if row.get('type') == 'cross_reference':
            ref_target = row.get('preferred_entry') if pd.notna(row.get('preferred_entry')) else row.get('related_subject')
            clean_target = re.sub(r'^(see\s+)?(also\s+)?(under\s+)?', '', str(ref_target), flags=re.IGNORECASE).strip()

            pre_list.append("")
            tgt_list.append(f"[CROSS-REFERENCE: Points to '{clean_target}']")
            post_list.append("")
            continue

        target_type, base_page = get_base_page(row.get('page'))
        if base_page is None:
            pre_list.append("")
            tgt_list.append("[ERROR: No valid page number found]")
            post_list.append("")
            continue

        target_idx = estimate_physical_page(target_type, base_page, arabic_map, roman_map, max_pages)

        if target_idx is None:
            pre_list.append("")
            tgt_list.append(f"[ERROR: Page {base_page} ({target_type}) out of bounds or impossible to estimate]")
            post_list.append("")
            continue

        tgt = get_clean_page_text(doc, target_idx)

        if not tgt.strip():
            tgt = f"[IMAGE OR BLANK PAGE: No readable text found on logical page {base_page}]"

        pre = get_substantial_page_text(doc, target_idx - 1, direction=-1)
        post = get_substantial_page_text(doc, target_idx + 1, direction=1)

        pre_list.append(pre)
        tgt_list.append(tgt)
        post_list.append(post)

    df['preceding_page'] = pre_list
    df['target_page'] = tgt_list
    df['succeeding_page'] = post_list

    for idx, row in df.iterrows():
        subj = str(row.get('main_subject', ''))
        sub_subj = str(row.get('sub_subject', ''))

        if "Asa-" in subj:
            df.at[idx, 'main_subject'] = subj.replace("Asa-", "Asadov")
            if "dov" in sub_subj:
                df.at[idx, 'sub_subject'] = ""

    df.to_csv(paths.context_csv, index=False)
    print(f"Extraction complete! Saved to {paths.context_csv}")

if __name__ == "__main__":
    process_corpus(parse_volume("Attach page text to each index entry."))
