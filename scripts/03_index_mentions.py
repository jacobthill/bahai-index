import hashlib
import json
import pandas as pd
import re
import time
from common import parse_volume, get_gemini_client, generate_json, DATA_DIR, GEMINI_MODEL

# --- CONFIGURATION ---
PREFIX_SUFFIX_LENGTH = 50
LIMIT = None  # Set to an integer to test a small batch (e.g., 20)

client = None  # Set in __main__ via get_gemini_client()

# Cache of successful Gemini answers, keyed by model + prompt, so re-runs only call Gemini
# for rows whose entry or page text changed. Delete the file to start fresh.
LLM_CACHE_JSONL = DATA_DIR / "cache" / "step03_llm.jsonl"
llm_cache = None
cache_stats = {'hits': 0, 'calls': 0}

def load_llm_cache():
    cache = {}
    if LLM_CACHE_JSONL.exists():
        with open(LLM_CACHE_JSONL, encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    cache[entry['key']] = entry['exact_substrings']
    return cache

def cached_llm_call(prompt):
    """Returns Gemini's exact_substrings for a prompt, from the cache when possible.
    Only successful answers are cached, so failures are retried on the next run."""
    global llm_cache
    if llm_cache is None:
        llm_cache = load_llm_cache()
    key = hashlib.sha256(f"{GEMINI_MODEL}\n{prompt}".encode('utf-8')).hexdigest()
    if key in llm_cache:
        cache_stats['hits'] += 1
        return llm_cache[key]
    cache_stats['calls'] += 1
    result = generate_json(client, prompt).get("exact_substrings", [])
    time.sleep(0.5)  # Rate limit protection; cached answers skip it
    llm_cache[key] = result
    LLM_CACHE_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(LLM_CACHE_JSONL, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'key': key, 'exact_substrings': result}, ensure_ascii=False) + "\n")
    return result

# --- PART 1: HEALING & WINDOWING ---

class LLMError(Exception):
    """Raised when the LLM could not be consulted (no key, API error, bad JSON)."""

def get_llm_mentions(full_subject, main_subject, text):
    """Uses LLM to find the exact string as it appears in the text.
    Returns a (possibly empty) list on success; raises LLMError on failure."""
    if not client:
        raise LLMError("No Gemini API key configured")

    prompt = f"""
    You are an expert historian. I have an index entry from a book, and the text of the page it points to.
    Your task is to find the EXACT, verbatim substring(s) in the TEXT that the index is referencing.

    Full Index Subject: '{full_subject}'
    Main Subject Focus: '{main_subject}'

    TEXT:
    {text}

    Respond ONLY with a valid JSON object containing a list of exact matching strings from the text.
    If the concept is discussed but the exact name isn't used, extract the closest verbatim phrase discussing the concept.
    Format: {{"exact_substrings": ["exact string from text here"]}}
    """
    try:
        return cached_llm_call(prompt)
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}") from e

def get_presentation_text(pre, tgt, post, term, exact_snippet):
    """Stitches pages and isolates the specific paragraph containing the exact snippet."""
    pre = str(pre) if pd.notna(pre) else ""
    tgt = str(tgt) if pd.notna(tgt) else ""
    post = str(post) if pd.notna(post) else ""

    # Join with a space to bridge the page gap
    full_text = f"{pre} {tgt} {post}"

    raw_blocks = [b.strip() for b in full_text.split('\n\n') if b.strip()]
    if not raw_blocks: return ""

    def should_merge(current, next_b):
        """Smarter heuristic to handle OCR breaks, abbreviations, and parentheticals."""
        if not next_b: return False
        c = current.rstrip()
        n = next_b.lstrip()
        if not c or not n: return False

        # 1. Next block starts with a lowercase letter
        if n[0].islower(): return True
        # 2. Unmatched opening parentheses/brackets
        if c.count('(') > c.count(')'): return True
        if c.count('[') > c.count(']'): return True
        # 3. Doesn't end with standard sentence punctuation
        if not re.search(r'[.!?"’]$', c): return True
        # 4. Ends with a single-letter abbreviation (e.g., "V.", "r.")
        if re.search(r'\b[a-zA-Z]\.$', c): return True
        # 5. Ends with common abbreviations
        if re.search(r'\b(i\.e|e\.g|vs|etc|cf|Prof|Dr|Mr|Mrs|Ms)\.$', c, re.IGNORECASE): return True

        return False

    paragraphs = []
    current = raw_blocks[0]
    for i in range(1, len(raw_blocks)):
        next_b = raw_blocks[i]
        if should_merge(current, next_b):
            current = current + " " + next_b
        else:
            paragraphs.append(current)
            current = next_b
    paragraphs.append(current)

    # Clean whitespace for reliable matching across healed paragraphs
    snippet_clean = " ".join(exact_snippet.split())

    target_idx = -1
    # 1. Primary Match: Find the healed paragraph that contains our raw paragraph snippet
    for i, p in enumerate(paragraphs):
        p_clean = " ".join(p.split())
        if snippet_clean and snippet_clean in p_clean:
            target_idx = i
            break

    # 2. Fallback: If healing severely mangled things, just find the term
    if target_idx == -1:
        for i, p in enumerate(paragraphs):
            if term in p:
                target_idx = i
                # If it's the very first paragraph, keep looking just in case there's a better one
                # deeper in the text, but hold onto it if it's the only one.
                if i != 0: break

    if target_idx == -1: return ""

    # Grab the target paragraph, plus one before and one after for context
    start_idx = max(0, target_idx - 1)
    end_idx = min(len(paragraphs), target_idx + 2)
    return "\n\n".join(paragraphs[start_idx:end_idx])

# --- PART 2: MAIN PROCESSING LOOP ---

def process(paths):
    paths.final_csv.parent.mkdir(parents=True, exist_ok=True)

    try:
        df = pd.read_csv(paths.context_csv)
    except FileNotFoundError:
        print(f"❌ Error: Could not find {paths.context_csv}. Did you run the text extraction step first?")
        return

    if LIMIT: df = df.head(LIMIT)

    print(f"Starting NLP Selector Processing for {len(df)} records...")
    new_rows = []

    # Tracking metrics
    stats = {'naive': 0, 'llm': 0, 'failed': 0, 'llm_error': 0, 'cross_ref': 0, 'missing_page': 0}

    for idx, row in df.iterrows():
        tgt = str(row.get('target_page', ''))
        pre = str(row.get('preceding_page', ''))
        post = str(row.get('succeeding_page', ''))

        # Sanitize messy OCR newlines to ensure consistent block splitting
        tgt = re.sub(r'\n\s*\n', '\n\n', tgt)
        pre = re.sub(r'\n\s*\n', '\n\n', pre)
        post = re.sub(r'\n\s*\n', '\n\n', post)

        # 1. HANDLE CROSS-REFERENCES
        if row.get('type') == 'cross_reference' or tgt.startswith('[CROSS-REFERENCE'):
            nr = row.copy()
            nr['target_selector'] = None
            nr['target_selector_prefix'] = None
            nr['target_selector_suffix'] = None
            nr['presentation_text'] = tgt
            new_rows.append(nr)
            stats['cross_ref'] += 1
            continue

        # 2. HANDLE MISSING OR BLANK PAGES
        if tgt.startswith('[ERROR') or tgt.startswith('[IMAGE'):
            nr = row.copy()
            nr['target_selector'] = None
            nr['target_selector_prefix'] = None
            nr['target_selector_suffix'] = None
            nr['presentation_text'] = tgt
            new_rows.append(nr)
            stats['missing_page'] += 1
            continue

        main_subject = str(row['main_subject']).strip()
        full_subject = str(row['full_subject']).strip()

        # 3. NAIVE STRING MATCHING (Fast Path)
        search_terms = []

        if main_subject in tgt:
            search_terms = [main_subject]
        elif full_subject in tgt:
            search_terms = [full_subject]
        elif ", " in main_subject:
            parts = main_subject.split(', ', 1)
            if len(parts) == 2:
                flipped = f"{parts[1]} {parts[0]}"
                if flipped in tgt: search_terms = [flipped]

        if search_terms:
            stats['naive'] += 1

        # 4. LLM FALLBACK (Smart Path)
        if not search_terms:
            try:
                search_terms = [t for t in get_llm_mentions(full_subject, main_subject, tgt) if t in tgt]
            except LLMError as e:
                # Record API failures distinctly so they aren't mistaken for "not in text"
                if client: print(f"  [!] LLM error on row {idx} ({full_subject}): {e}")
                nr = row.copy()
                nr['target_selector'] = None
                nr['target_selector_prefix'] = None
                nr['target_selector_suffix'] = None
                nr['presentation_text'] = f"[LLM ERROR: {e}]"
                new_rows.append(nr)
                stats['llm_error'] += 1
                if client: time.sleep(0.5)
                continue
            if search_terms:
                stats['llm'] += 1

        # 5. FAILURE TO LOCATE
        if not search_terms:
            nr = row.copy()
            nr['target_selector'] = None
            nr['target_selector_prefix'] = None
            nr['target_selector_suffix'] = None
            nr['presentation_text'] = "[TEXT NOT LOCATED: The concept may be discussed implicitly, or the OCR is corrupted.]"
            new_rows.append(nr)
            stats['failed'] += 1
            continue

        # 6. SUCCESSFUL EXTRACTION
        for term in set(search_terms):
            match = re.search(re.escape(term), tgt)
            if match:
                start, end = match.start(), match.end()
                nr = row.copy()
                nr['target_selector'] = term

                nr['target_selector_prefix'] = tgt[max(0, start-PREFIX_SUFFIX_LENGTH):start]
                nr['target_selector_suffix'] = tgt[end:end+PREFIX_SUFFIX_LENGTH]

                # Extract the exact raw paragraph from tgt, bounded by \n\n
                blocks = tgt.split('\n\n')
                exact_snippet = ""
                char_count = 0
                for b in blocks:
                    block_len = len(b)
                    if char_count <= start <= char_count + block_len:
                        exact_snippet = b.strip()
                        break
                    char_count += block_len + 2

                nr['presentation_text'] = get_presentation_text(pre, tgt, post, term, exact_snippet)
                new_rows.append(nr)

    final_df = pd.DataFrame(new_rows)
    final_df.to_csv(paths.final_csv, index=False)

    print("\n--- EXTRACTION SUMMARY ---")
    print(f"Total Rows Output:  {len(final_df)}")
    print(f"Cross References:   {stats['cross_ref']}")
    print(f"Missing/Images:     {stats['missing_page']}")
    print(f"Naive Matches:      {stats['naive']}")
    print(f"LLM Rescues:        {stats['llm']}")
    print(f"Failed to Locate:   {stats['failed']}")
    print(f"LLM Errors:         {stats['llm_error']}")
    if stats['llm_error']:
        print(f"⚠️  {stats['llm_error']} rows were not checked by the LLM and are marked '[LLM ERROR: ...]'. Fix the cause and re-run.")
    print(f"Gemini calls:       {cache_stats['calls']} ({cache_stats['hits']} answered from cache)")
    print(f"Data saved to {paths.final_csv}")

if __name__ == "__main__":
    paths = parse_volume("Locate each index entry's mention in its page text.")
    client = get_gemini_client()
    process(paths)
