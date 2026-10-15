import fitz
import re
import pandas as pd
from common import volume_paths

def strip_header(text, y_coord):
    if y_coord < 150:
        text = re.sub(r'Vol\s+\d+\s+References', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s+[AP]M', '', text, flags=re.IGNORECASE)
        text = re.sub(r'Page\s+\d+', '', text, flags=re.IGNORECASE)
    return text

def is_page_num(text_str):
    text_str = text_str.strip(" \t\n\r,.;")
    if not text_str: return False
    if re.match(r'^\d+n?\d*$', text_str): return True
    if re.match(r'^\d+[-–—\u2013\u2014]\d+$', text_str): return True
    if re.match(r'^(x{0,3})(ix|iv|v?i{0,3})$', text_str) and text_str.islower() and len(text_str) > 0:
        return True
    return False

def _append_to_final(data_list, main, sub, numbers):
    if not numbers: return

    main_clean = main.strip(" ,;:\t\n\r")
    sub_clean = sub.strip(" ,;:\t\n\r")
    full = f"{main_clean}: {sub_clean}" if sub_clean else main_clean

    for n, p_type in numbers:
        data_list.append({
            "main_subject": main_clean,
            "sub_subject": sub_clean,
            "full_subject": full,
            "page": n,
            "type": p_type,
            "related_subject": None,
            "preferred_entry": None
        })

def update_subjects(main, sub, text, is_baseline, entry_done, last_had_nums, hyphen_re, unfinished_re):
    if not text or not text.strip(): return main, sub
    clean_text = text.strip()

    if is_baseline and not re.match(r'^\d', clean_text):
        return clean_text, ""

    target = sub if sub else main
    is_unfinished = unfinished_re.search(target) is not None and not entry_done
    is_hyphenated = hyphen_re.search(target) is not None

    # ENCLOSURE TRACKING: Check if we are currently inside unclosed parentheses or brackets
    has_unclosed_paren = target.count('(') > target.count(')')
    has_unclosed_bracket = target.count('[') > target.count(']')
    is_mid_enclosure = has_unclosed_paren or has_unclosed_bracket

    if is_hyphenated:
        if clean_text[0].islower() and not re.search(r'\b(al|Dawlih|Shaykh)$', hyphen_re.sub('', target).strip(), re.IGNORECASE):
            new_val = hyphen_re.sub('', target).strip() + clean_text
        else:
            new_val = hyphen_re.sub('-', target).strip() + clean_text
        return (main, new_val) if sub else (new_val, "")

    # Force append if the string ends mid-parenthesis or bracket
    elif is_unfinished or is_mid_enclosure:
        new_val = f"{target} {clean_text}".strip()
        return (main, new_val) if sub else (new_val, "")

    if entry_done or last_had_nums or (not sub and not is_baseline):
        return main, clean_text
    else:
        new_val = f"{target} {clean_text}".strip()
        return (main, new_val) if sub else (new_val, "")

def parse_hierarchical_index(pdf_path, index_pages, initial_main_subject=""):
    doc = fitz.open(pdf_path)
    final_data = []

    HYPHEN_SIGNAL = re.compile(r'[\-\u00ad\u2013\u2014]\s*$')
    UNFINISHED_SIGNAL = re.compile(r'\b(bin|ibn|Abu|Umm|Aqa|Haji|Sayyid|Mirza|Ustad|of|for|and|the|al|in|to|Am)\s*$', re.IGNORECASE)
    NOISE_LOWER = [n.lower() for n in ["INDEX", "BAHA’IS", "TRANSCASPIA", "CAUCASUS", "numbers denote", "italic numbers", "references"]]

    main_subject = initial_main_subject
    sub_subject = ""
    last_line_had_numbers = False
    entry_completed = False

    for page_num in index_pages:
        try:
            page = doc.load_page(page_num - 1)
        except: continue

        blocks = page.get_text("dict")["blocks"]
        width = page.rect.width
        all_lines = []
        for b in blocks:
            for l in b.get("lines", []): all_lines.append(l)

        left_lines = sorted([l for l in all_lines if l["spans"][0]["origin"][0] < width / 2], key=lambda x: x["bbox"][1])
        right_lines = sorted([l for l in all_lines if l["spans"][0]["origin"][0] >= width / 2], key=lambda x: x["bbox"][1])

        # NEW ROBUST MARGIN DETECTION
        master_y = None
        for line in left_lines:
            span = line["spans"][0]
            y_coord = span["origin"][1]
            if y_coord < 80: continue # Hard skip absolute top-of-page headers
            text = strip_header(span["text"], y_coord).strip()

            # Find the first real line of text to establish the Y-cutoff
            if len(text) > 2 and not text.isupper() and not any(n in text.lower() for n in NOISE_LOWER):
                if not re.match(r'^[\d\W]+$', text):
                    master_y = y_coord - 2
                    break

        if master_y is None: continue

        # Calculate the TRUE column margins using the minimum X coordinate
        left_xs = [l["spans"][0]["origin"][0] for l in left_lines if l["spans"][0]["origin"][1] >= master_y and len(l["spans"][0]["text"].strip()) > 1 and not re.match(r'^\d', l["spans"][0]["text"].strip())]
        if not left_xs: continue
        col1_x = min(left_xs)

        right_xs = [l["spans"][0]["origin"][0] for l in right_lines if l["spans"][0]["origin"][1] >= master_y and len(l["spans"][0]["text"].strip()) > 1 and not re.match(r'^\d', l["spans"][0]["text"].strip())]

        col_data = [(left_lines, col1_x)]
        if right_xs:
            col_data.append((right_lines, min(right_xs)))

        for col_lines, baseline_x in col_data:
            for line in col_lines:
                spans = line["spans"]
                if not spans or spans[0]["origin"][1] < master_y: continue

                is_at_baseline = abs(spans[0]["origin"][0] - baseline_x) < 7
                current_text_buffer, current_numbers = [], []

                for s in spans:
                    raw_text = strip_header(s["text"], s["origin"][1])
                    p_type = "biographical_note" if s["flags"] & 16 else ("photograph" if s["flags"] & 2 else "regular")

                    raw_text = re.sub(r'Am[\s\u200b\u200c\xad]+in\b', 'Amin', raw_text)
                    raw_text = re.sub(r'([a-zA-Z\)])[\s\xad\u200b]*(\d+)', r'\1 \2', raw_text)

                    tokens = re.split(r'(\s+)', raw_text)
                    for t in tokens:
                        t_clean = t.strip(" \t\n\r,;:")
                        if not t_clean:
                            if t and current_text_buffer and not current_text_buffer[-1].endswith(" "):
                                current_text_buffer.append(" ")
                            continue

                        if t_clean.lower() == "see":
                            if current_numbers:
                                _append_to_final(final_data, main_subject, sub_subject, current_numbers)
                                current_numbers = []
                            remaining_line = "".join([sp["text"] for sp in spans[spans.index(s):]])
                            cross_ref_content = re.sub(r'^.*?see', 'see', remaining_line, flags=re.IGNORECASE).strip()
                            cross_ref_content = re.sub(r'Am[\s\u200b\u200c\xad]+in\b', 'Amin', cross_ref_content)

                            main_clean = main_subject.strip(" ,;:\t\n\r")
                            sub_clean = sub_subject.strip(" ,;:\t\n\r")
                            full_sub = f"{main_clean}: {sub_clean}" if sub_clean else main_clean

                            final_data.append({
                                "main_subject": main_clean, "sub_subject": sub_clean,
                                "full_subject": full_sub, "page": None, "type": "cross_reference",
                                "related_subject": re.sub(r'(?i)^see\s+also\s*', '', cross_ref_content).strip(':.,; ') if "also" in cross_ref_content.lower() else None,
                                "preferred_entry": re.sub(r'(?i)^see\s*', '', cross_ref_content).strip(':.,; ') if "also" not in cross_ref_content.lower() else None
                            })
                            entry_completed, current_text_buffer = True, []
                            break

                        if is_page_num(t_clean):
                            current_numbers.append((t_clean, p_type))
                        else:
                            if current_numbers:
                                snippet = "".join(current_text_buffer).strip()
                                main_subject, sub_subject = update_subjects(main_subject, sub_subject, snippet, is_at_baseline, entry_completed, last_line_had_numbers, HYPHEN_SIGNAL, UNFINISHED_SIGNAL)
                                _append_to_final(final_data, main_subject, sub_subject, current_numbers)
                                current_numbers, current_text_buffer, entry_completed = [], [t], True
                            else:
                                current_text_buffer.append(t)
                    else: continue
                    break

                if current_text_buffer or current_numbers:
                    remaining = "".join(current_text_buffer).strip()
                    if remaining:
                        main_subject, sub_subject = update_subjects(main_subject, sub_subject, remaining, is_at_baseline, entry_completed, last_line_had_numbers, HYPHEN_SIGNAL, UNFINISHED_SIGNAL)
                    if current_numbers:
                        _append_to_final(final_data, main_subject, sub_subject, current_numbers)
                        entry_completed, last_line_had_numbers = True, True
                    else: last_line_had_numbers = False

    # POST-CLEANUP
    for entry in final_data:
        for field in ["main_subject", "sub_subject", "full_subject"]:
            if entry.get(field):
                val = entry[field]

                # 1. Target split anomalies aggressively
                val = re.sub(r'Am[\s\u200b\u200c\xad]+in\b', 'Amin', val)
                val = re.sub(r'\bAmin\s+al\s+-\b', 'Amin al-', val)
                val = re.sub(r'\b(ofthe|andthe|inthe|tothe|forthe)\b', lambda m: m.group(1)[:-3] + ' the', val, flags=re.IGNORECASE)

                # 2. Split CamelCase (e.g., ofBaha'is -> of Baha'is)
                val = re.sub(r'(?<!\bMc)(?<!\bMac)([a-z])([A-Z])', r'\1 \2', val)

                # 3. Targeted un-gluing for specific known PDF errors
                val = re.sub(r'(?i)\b(persecution|attitude|execution|arrival)of\b', r'\1 of', val)

                # 4. Target acronyms broken by the CamelCase logic
                val = re.sub(r'\bTs\s+K\b', 'TsK', val)

                entry[field] = val.replace('\u00ad', '').strip().replace('  ', ' ')

    return final_data

if __name__ == "__main__":
    paths = volume_paths("2")
    FILE_PATH = paths.pdf
    OUT_PATH = paths.extracted_csv
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAGES = list(range(229, 337))
    extracted = parse_hierarchical_index(FILE_PATH, PAGES)
    pd.DataFrame(extracted).to_csv(OUT_PATH, index=False)
    print(f"Extraction complete. Total rows: {len(extracted)}")
