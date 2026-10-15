"""Diagnostic audit of step 03 output. Exits 1 if any warnings are found."""
import pandas as pd
import re
import sys
from common import parse_volume

def print_header(title):
    print(f"\n{'='*50}")
    print(f" {title}")
    print(f"{'='*50}")

def is_roman_numeral(s):
    s = str(s).strip(" \t\n\r,.;()[]").lower()
    if not s: return False
    return bool(re.match(r'^(l?x{0,3})(ix|iv|v?i{0,3})$', s))

def normalize_whitespace(text):
    """Flattens all newlines, tabs, and multiple spaces into single spaces for fair comparison."""
    if not isinstance(text, str) or text.lower() == 'nan': return ""
    return " ".join(text.split())

def verify_context(row):
    """
    Validates that selectors exist (ignoring whitespace differences)
    and the presentation text structure is valid.
    """
    raw_text = str(row.get('presentation_text', ''))
    sel = str(row.get('target_selector', ''))
    pre = str(row.get('target_selector_prefix', ''))
    suf = str(row.get('target_selector_suffix', ''))

    # Skip validation for known missing/error rows
    if raw_text.startswith(('[TEXT NOT LOCATED', '[CROSS-REFERENCE', '[ERROR', '[LLM ERROR')):
        return [], None, None

    norm_text = normalize_whitespace(raw_text)
    norm_sel = normalize_whitespace(sel)

    # Normalize FIRST, then slice. This prevents \n characters from eating the slice budget.
    full_norm_pre = normalize_whitespace(pre)
    full_norm_suf = normalize_whitespace(suf)

    # Check the last 15 chars of prefix, and first 15 chars of suffix
    norm_pre = full_norm_pre[-15:] if full_norm_pre else ""
    norm_suf = full_norm_suf[:15] if full_norm_suf else ""

    missing_selectors = []
    if norm_sel and norm_sel not in norm_text: missing_selectors.append('selector')
    if norm_pre and norm_pre not in norm_text: missing_selectors.append('prefix')
    if norm_suf and norm_suf not in norm_text: missing_selectors.append('suffix')

    # Paragraph validation
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', raw_text) if p.strip()]
    num_p = len(paragraphs)

    structure_err = None

    # The excerpt is the paragraphs the selector spans, plus one before and one after
    sel_paragraphs = len([p for p in re.split(r'\n\s*\n', sel) if p.strip()]) or 1
    max_p = sel_paragraphs + 2

    if num_p == 0:
        structure_err = "No paragraphs found"
    elif num_p > max_p:
        structure_err = f"Expected max {max_p} paragraphs, found {num_p}"
    elif norm_sel:
        found_in_valid_p = False

        # The selector starts in the first paragraph (excerpt begins at the mention)
        # or the second (one paragraph of context before it)
        valid_indices = [0, 1] if num_p >= 2 else [0]

        # Check if the full selector OR any of its valid chunks anchor in the expected paragraph
        sel_chunks = [normalize_whitespace(p) for p in re.split(r'\n\s*\n', sel) if p.strip()]

        for i in valid_indices:
            p_norm = normalize_whitespace(paragraphs[i])
            if norm_sel in p_norm or any(chunk in p_norm for chunk in sel_chunks if chunk):
                found_in_valid_p = True
                break

        if not found_in_valid_p:
            structure_err = f"Target selector not found in the expected paragraph (Total paragraphs: {num_p})"

    # Return raw text snippets for debugging
    debug_info = {
        'sel': sel, 'pre': pre, 'suf': suf, 'text_snippet': raw_text[:200] + "..."
    }

    return missing_selectors, structure_err, debug_info

def audit_pagination(paths):
    """Prints the audit report and returns True only if no warnings were found."""
    csv_path = paths.final_csv

    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        print(f"❌ File not found: {csv_path}")
        return False

    print_header(f"PAGINATION & CONTEXT DIAGNOSTIC AUDIT: VOLUME {paths.vol}")

    # Rows step 03 could not check because the Gemini call failed
    llm_errors = df['presentation_text'].astype(str).str.startswith('[LLM ERROR').sum()

    testable_rows = df[
        (~df['target_page'].astype(str).str.startswith('[', na=False)) &
        (df['target_page'].notna()) &
        (df['type'] != 'cross_reference')
    ]

    if testable_rows.empty:
        print("No testable standard rows found.")
        return False

    total_tested = 0
    hits, shifted_early, shifted_late, poisoned, pure_misses = 0, [], [], [], []
    context_issues = []

    for idx, row in testable_rows.iterrows():
        page_str = str(row['page']).strip().lower()

        match_arabic = re.search(r'^(\d+)', page_str)
        match_roman = re.search(r'^([xivlc]+)', page_str)

        if match_arabic:
            base_page, page_type = match_arabic.group(1), 'arabic'
        elif match_roman and is_roman_numeral(match_roman.group(1)):
            base_page, page_type = match_roman.group(1), 'roman'
        else:
            continue

        total_tested += 1

        # 1. Context & Selector Validation
        missing_sels, struct_err, debug_info = verify_context(row)

        if missing_sels or struct_err:
            context_issues.append({
                'row': idx,
                'page': base_page,
                'missing': missing_sels,
                'struct': struct_err,
                'debug': debug_info
            })

        # 2. Pagination Check
        target_text = str(row.get('target_page', ''))
        pre_text = str(row.get('preceding_page', ''))
        post_text = str(row.get('succeeding_page', ''))
        pattern = rf'\b{base_page}\b'

        def check_text(text):
            if not text or text.lower() == 'nan': return False
            return bool(re.search(pattern, text[:250]) or re.search(pattern, text[-250:]))

        if check_text(target_text):
            hits += 1
        elif check_text(pre_text):
            shifted_early.append({'row': idx, 'page': base_page, 'subject': row.get('main_subject', '')})
        elif check_text(post_text):
            shifted_late.append({'row': idx, 'page': base_page, 'subject': row.get('main_subject', '')})
        else:
            top_target = target_text[:100].upper()
            if page_type == 'arabic' and any(word in top_target for word in ['ILLUSTRATION', 'CONTENT', 'PREFACE', 'INTRODUCTION']):
                poisoned.append({'row': idx, 'page': base_page, 'subject': row.get('main_subject', '')})
            else:
                pure_misses.append({'row': idx, 'page': base_page, 'subject': row.get('main_subject', '')})

    # Output generation
    if total_tested == 0:
        print("No rows with a parseable page number found.")
        return False

    hit_rate = (hits / total_tested) * 100
    context_rate = ((total_tested - len(context_issues)) / total_tested) * 100

    print(f"Total Pages Audited: {total_tested}")
    print(f"🎯 Perfect Hits (On Target Page):       {hits} ({hit_rate:.1f}%)")
    print(f"📄 Context/Selector Integrity:          {total_tested - len(context_issues)} ({context_rate:.1f}%)")
    print(f"⚠️ Shifted Early/Late:                  {len(shifted_early)} / {len(shifted_late)}")
    print(f"☠️ Poisoned/Missed:                     {len(poisoned)} / {len(pure_misses)}")
    print(f"🤖 LLM Errors (unchecked rows):         {llm_errors}")

    passed = hit_rate >= 90.0 and len(context_issues) == 0 and llm_errors == 0
    if passed:
        print("\n✅ PASS: Diagnostic checks complete with no warnings.")
    else:
        print("\n❌ DIAGNOSTIC WARNINGS FOUND:")
        if llm_errors:
            print(f"\n--- 🤖 {llm_errors} rows marked '[LLM ERROR]': fix the cause and re-run step 03 ---")

    if context_issues:
        print("\n--- 📄 CONTEXT & SELECTOR FAILURES ---")
        for f in context_issues[:5]:
            issues = []
            if f['missing']: issues.append(f"Missing: {', '.join(f['missing'])}")
            if f['struct']: issues.append(f"Structure: {f['struct']}")
            print(f"\nRow {f['row']} | Page: {f['page']} | {' | '.join(issues)}")

            # Print debug text so you can see exactly what went wrong
            print(f"   -> Target Selector: '{f['debug']['sel']}'")
            if f['missing']:
                print(f"   -> Looked for Prefix: '{f['debug']['pre']}'")
                print(f"   -> Looked for Suffix: '{f['debug']['suf']}'")
            if f['struct']:
                print(f"   -> Preview of text generated:\n   {f['debug']['text_snippet']}")

    return passed

if __name__ == "__main__":
    paths = parse_volume("Audit step 03 output for pagination and selector problems.")
    sys.exit(0 if audit_pagination(paths) else 1)
