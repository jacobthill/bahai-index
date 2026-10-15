import fitz
import re
import pandas as pd
from collections import Counter
from common import volume_paths

def is_page_num(text_str):
    """Robust checker for standard digits, ranges, and roman numerals."""
    # Aggressively strip invisible OCR characters to prevent false negatives
    text_str = text_str.strip(" \t\n\r,.;\xa0\u200b\xad")
    if not text_str: return False

    # Match standalone digits, footnote formats (e.g., 214n47), and ranges
    if re.match(r'^\d+n?\d*$', text_str) or re.match(r'^\d+[-–—\u2013\u2014]\d+$', text_str):
        # Extract the base number to check if it's actually a year
        base_num = int(re.search(r'^(\d+)', text_str).group(1))
        if base_num > 1000:
            return False # Reject years (e.g., 1844, 1914)
        return True

    if re.match(r'^(x{0,3})(ix|iv|v?i{0,3})$', text_str) and text_str.islower() and len(text_str) > 0:
        return True

    return False

def parse_hierarchical_index(pdf_path, index_pages, initial_main_subject=""):
    doc = fitz.open(pdf_path)
    final_data = []

    # Matches standard/soft/en/em hyphens at the end of a line
    HYPHEN_SIGNAL = re.compile(r'[\-\u00ad\u2013\u2014]\s*$')

    # Articles that frequently cause line-break splits
    ARTICLE_SIGNAL = re.compile(r'\b(al|Dawlih|Shaykh|al-)\s*$', re.IGNORECASE)

    # Grammatical signals that a line is unfinished and must continue
    UNFINISHED_SIGNAL = re.compile(r'\b(bin|ibn|Abu|Umm|Aqa|Haji|Sayyid|Mirza|Ustad|of|for|and|the|Oriental|Imperial|Russian|Archaeological|Military-Historical|Historical|Department|al|Deutschen|Morgenländischen|Imperatorskogo|russkogo|arkheologicheskogo|Kavkazskogo|otdela|Evgrafovich|Saltykov|Fazili?|Mu.ayyid)\s*$', re.IGNORECASE)

    # Words that indicate the start of a continuation of the previous title/name
    CONTINUATION_START = re.compile(r'^[\(\[\‘\’\'\"]*(Khan|Beg|Pasha|Dawlih|Mulk|Sultan|Shah|Zadih|Mirza|Society|Languages|Company|Association|Committee|University|Gesellschaft|ZDMG|obshchestva|Saltykov|Archive|al-Saltanih|Saltanih|Ruhani|Nabili?|Akbar)\b', re.IGNORECASE)

    NOISE = ["INDEX", "BAHA’IS", "TRANSCASPIA", "CAUCASUS", "numbers denote", "italic numbers"]

    # State variables
    main_subject = initial_main_subject
    sub_subject = ""
    last_line_had_numbers = False
    cross_ref_mode = None
    cross_ref_buffer = ""

    for page_num in index_pages:
        try:
            page = doc.load_page(page_num - 1)
        except:
            continue

        blocks = page.get_text("dict")["blocks"]
        width = page.rect.width

        # 1. ORGANIZE LINES BY COLUMN
        all_lines = []
        for b in blocks:
            for l in b.get("lines", []):
                all_lines.append(l)

        left_lines = [l for l in all_lines if l["spans"][0]["origin"][0] < width / 2]
        right_lines = [l for l in all_lines if l["spans"][0]["origin"][0] >= width / 2]
        left_lines.sort(key=lambda x: x["bbox"][1])
        right_lines.sort(key=lambda x: x["bbox"][1])

        # 2. FIND MASTER ANCHOR
        master_y = None
        col1_x = None
        for line in left_lines:
            span = line["spans"][0]
            text = span["text"].strip()
            if len(text) > 2 and not text.isupper() and not any(n in text for n in NOISE):
                if not re.match(r'^\d', text):
                    master_y = span["origin"][1] - 2
                    col1_x = span["origin"][0]
                    break
        if master_y is None: continue

        # 3. FIND TRUE BASELINES USING MIN()
        left_starts = [round(l["spans"][0]["origin"][0]) for l in left_lines
                       if l["spans"][0]["origin"][1] >= master_y and not re.match(r'^\d', l["spans"][0]["text"])]
        right_starts = [round(l["spans"][0]["origin"][0]) for l in right_lines
                        if l["spans"][0]["origin"][1] >= master_y and not re.match(r'^\d', l["spans"][0]["text"])]

        if not left_starts: continue
        col1_x = min(left_starts)

        if not right_starts:
            col_data = [(left_lines, col1_x)]
        else:
            col2_x = min(right_starts)
            col_data = [(left_lines, col1_x), (right_lines, col2_x)]

        # 4. HIERARCHICAL PROCESSING
        for col_lines, baseline_x in col_data:
            for line in col_lines:
                spans = line["spans"]
                if not spans: continue

                first_text = spans[0]["text"].strip()
                x0 = spans[0]["origin"][0]
                y0 = spans[0]["origin"][1]

                if y0 < master_y or any(n in first_text for n in NOISE) or (first_text.isupper() and len(first_text) > 1):
                    continue

                is_at_baseline = abs(x0 - baseline_x) < 7
                starts_with_digit = re.match(r'^\d', first_text) is not None

                # B. Token-Based Extraction (NEW L-to-R Forward Scanner)
                extracted_text_buffer = []
                trailing_text_buffer = []
                line_numbers = []
                found_numbers = False

                for s in spans:
                    raw_text = s["text"]
                    if not raw_text.strip(): continue
                    p_type = "biographical_note" if s["flags"] & 16 else ("photograph" if s["flags"] & 2 else "regular")

                    # V2 FIX: Un-glue letters and digits before splitting
                    raw_text = re.sub(r'([a-zA-Z\)])[\s\xad\u200b]*(\d+)', r'\1 \2', raw_text)
                    raw_text = re.sub(r'(\d+)n\s+(\d+)', r'\1n\2', raw_text)

                    sub_tokens = re.split(r'([\s,;]+)', raw_text)
                    for token in sub_tokens:
                        if re.match(r'^[\s,;]+$', token):
                            if trailing_text_buffer:
                                trailing_text_buffer.append(token)
                            elif found_numbers:
                                pass # Spacing between numbers is handled implicitly
                            else:
                                extracted_text_buffer.append(token)
                            continue

                        clean_tok = token.strip(" \t\n\r,.;\xa0\u200b\xad")
                        if not clean_tok: continue

                        if is_page_num(clean_tok):
                            found_numbers = True
                            line_numbers.append((clean_tok, p_type))
                        else:
                            # If text is encountered AFTER the first page number
                            if found_numbers:
                                trailing_text_buffer.append(token)
                            else:
                                extracted_text_buffer.append(token)

                extracted_text = "".join(extracted_text_buffer).strip(" ,;")
                extracted_text = re.sub(r'\s+', ' ', extracted_text)

                trailing_text = "".join(trailing_text_buffer).strip(" ,;")
                trailing_text = re.sub(r'\s+', ' ', trailing_text)

                has_numbers = len(line_numbers) > 0

                # C. Cross-Reference Routing Logic
                full_text_for_cross_ref = extracted_text
                if trailing_text:
                    full_text_for_cross_ref = f"{extracted_text} {trailing_text}".strip()

                see_match = re.search(r'\bsee(\s+also)?\b', full_text_for_cross_ref, re.IGNORECASE)

                if cross_ref_mode and (is_at_baseline or has_numbers or see_match):
                    full_subject = f"{main_subject}: {sub_subject}" if sub_subject else main_subject
                    final_data.append({
                        "main_subject": main_subject.strip(", "),
                        "sub_subject": sub_subject.strip(", "),
                        "full_subject": full_subject.strip(", "),
                        "page": None,
                        "type": "cross_reference",
                        "related_subject": cross_ref_buffer if cross_ref_mode == "related_subject" else None,
                        "preferred_entry": cross_ref_buffer if cross_ref_mode == "preferred_entry" else None
                    })
                    cross_ref_mode = None
                    cross_ref_buffer = ""

                if see_match:
                    split_idx = see_match.start()
                    subject_part = full_text_for_cross_ref[:split_idx].strip(' ;.,')
                    cross_ref_part = full_text_for_cross_ref[split_idx:].strip()
                    extracted_text = subject_part # Overwrite so subject stays clean
                    if re.match(r'(?i)^see\s+also', cross_ref_part):
                        cross_ref_mode = "related_subject"
                        cross_ref_buffer = re.sub(r'(?i)^see\s+also\s*', '', cross_ref_part).strip(':.,; ')
                    else:
                        cross_ref_mode = "preferred_entry"
                        cross_ref_buffer = re.sub(r'(?i)^see\s*', '', cross_ref_part).strip(':.,; ')
                elif cross_ref_mode and not has_numbers and not is_at_baseline:
                    if HYPHEN_SIGNAL.search(cross_ref_buffer) and extracted_text and extracted_text[0].islower() and not ARTICLE_SIGNAL.search(cross_ref_buffer):
                        cross_ref_buffer = HYPHEN_SIGNAL.sub('', cross_ref_buffer) + extracted_text
                    else:
                        cross_ref_buffer = f"{cross_ref_buffer} {extracted_text}".strip()
                    extracted_text = ""

                # D. Updated State Machine: Grammatical Healing
                if is_at_baseline and not starts_with_digit and extracted_text:
                    main_subject = extracted_text
                    sub_subject = ""
                elif not starts_with_digit and extracted_text:
                    target = sub_subject if sub_subject else main_subject

                    # --- Signal Detection ---
                    is_hyphenated = HYPHEN_SIGNAL.search(target) is not None
                    is_article = ARTICLE_SIGNAL.search(target) is not None
                    is_unfinished = UNFINISHED_SIGNAL.search(target) is not None

                    starts_lower = extracted_text[0].islower() if extracted_text else False
                    starts_with_paren = extracted_text.startswith("(")
                    starts_with_title = CONTINUATION_START.search(extracted_text) is not None

                    in_parens = "(" in target and ")" not in target
                    ends_with_comma = target.endswith(",")

                    # Action 1: Force Merge (Strip Hyphen, No Space)
                    if (is_hyphenated and starts_lower and not is_article) or (in_parens and starts_lower):
                        new_val = HYPHEN_SIGNAL.sub('', target) + extracted_text
                        if sub_subject: sub_subject = new_val
                        else: main_subject = new_val

                    # Action 2: Force Append (Keep Text, Manage Spacing)
                    elif is_article or is_unfinished or starts_with_paren or starts_with_title or ends_with_comma or in_parens or is_hyphenated:
                        if is_hyphenated:
                            new_val = f"{HYPHEN_SIGNAL.sub('-', target).rstrip()}{extracted_text}".strip()
                        else:
                            new_val = f"{target} {extracted_text}".strip()

                        if sub_subject: sub_subject = new_val
                        else: main_subject = new_val

                    # Standard Sub-Subject Logic
                    elif last_line_had_numbers or (not sub_subject and not is_at_baseline):
                        sub_subject = extracted_text

                    # Standard Append
                    else:
                        if sub_subject: sub_subject = f"{sub_subject} {extracted_text}".strip()
                        else: main_subject = f"{main_subject} {extracted_text}".strip()

                # E. Append Standard Data
                if has_numbers:
                    full_subject = f"{main_subject}: {sub_subject}" if sub_subject else main_subject
                    for n, p_type in line_numbers:
                        final_data.append({
                            "main_subject": main_subject.strip(", "),
                            "sub_subject": sub_subject.strip(", "),
                            "full_subject": full_subject.strip(", "),
                            "page": n,
                            "type": p_type,
                            "related_subject": None,
                            "preferred_entry": None
                        })

                # F. Update State
                if has_numbers:
                    last_line_had_numbers = True
                elif extracted_text or (cross_ref_mode and not has_numbers):
                    last_line_had_numbers = False

    # G. Post-Processing Cleanup
    for entry in final_data:
        for field in ["main_subject", "sub_subject", "full_subject"]:
            if entry.get(field):
                val = entry[field]
                val = re.sub(r'(\w)[\-\u00ad\u2013\u2014]\s*([a-z])', r'\1\2', val)

                # V2 FIX: Un-glue texts that were broken across different PDF spans
                val = re.sub(r'([a-zA-Z\)])(\d+)', r'\1 \2', val)
                val = re.sub(r'(\d+)n\s+(\d+)', r'\1n\2', val)

                # V2 FIX: Target split anomalies aggressively
                val = re.sub(r'\b(ofthe|andthe|inthe|tothe|forthe)\b', lambda m: m.group(1)[:-3] + ' the', val, flags=re.IGNORECASE)

                # V2 FIX: Split CamelCase
                val = re.sub(r'(?<!\bMc)(?<!\bMac)([a-z])([A-Z])', r'\1 \2', val)

                # V2 FIX: Targeted un-gluing for specific known PDF errors
                val = re.sub(r'(?i)\b(persecution|attitude|execution|arrival)of\b', r'\1 of', val)

                val = re.sub(r'Am[\s\u200b\u200c\xad]+in\b', 'Amin', val)
                val = re.sub(r'\bAmin\s+al\s+-\b', 'Amin al-', val)

                entry[field] = val.replace('\u00ad', '').strip().replace('  ', ' ')

    return final_data

# --- Execution ---
if __name__ == "__main__":
    paths = volume_paths("1")
    FILE_PATH = paths.pdf
    OUT_PATH = paths.extracted_csv
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAGES = list(range(290, 310))

    extracted = parse_hierarchical_index(FILE_PATH, PAGES)
    df = pd.DataFrame(extracted)
    cols = ['main_subject', 'sub_subject', 'full_subject', 'page', 'type', 'related_subject', 'preferred_entry']
    df = df[[c for c in cols if c in df.columns]]
    df.to_csv(OUT_PATH, index=False)
    print("v1 Extraction complete.")
