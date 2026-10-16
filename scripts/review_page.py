"""Writes a readable review page for one batch: data/review/batch-<n>.html.

    uv run python scripts/review_page.py <batch number>

For each row of the batch: the decision, the draft record (names, description with its source,
dates, coordinates, relationships, draft_notes), and the passages the draft was based on, with
the mention highlighted. The page is for reading only; make decisions in curated/review.csv and
edit records in curated/drafts.jsonl.
"""
import argparse
import html
import re
from common import DATA_DIR
from entities import load_review, decision, load_drafts
from review import load_entries, evidence_for

CSS = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --card:#f5f5f7; --line:#d2d2d7; --mark:#fff3b0; --warn:#b3261e; --ok:#1b6e3a; }
@media (prefers-color-scheme: dark) { :root { --bg:#161617; --fg:#f5f5f7; --muted:#a1a1a6; --card:#232325; --line:#3a3a3c; --mark:#5c4b00; --warn:#ff8a80; --ok:#7bd88f; } }
body { background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system, system-ui, sans-serif; max-width:920px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:0 0 4px; } .sub { color:var(--muted); margin-bottom:24px; }
nav a { display:inline-block; margin:0 10px 6px 0; color:var(--fg); }
section { border-top:1px solid var(--line); padding:20px 0; }
h2 { font-size:18px; margin:0 0 6px; } .meta { color:var(--muted); font-size:13px; margin-bottom:10px; }
.card { background:var(--card); border-radius:10px; padding:12px 14px; margin:10px 0; }
.label { font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }
.warn { color:var(--warn); font-weight:600; } .ok { color:var(--ok); }
blockquote { margin:8px 0 14px; padding:8px 12px; border-left:3px solid var(--line); }
mark { background:var(--mark); color:inherit; padding:0 2px; border-radius:3px; }
code { font-size:13px; }
"""

def decided_id(row):
    """The entity ID a row's decision points to, or '' (undecided or ignored)."""
    d = decision(row) or ""
    return d.split(":", 1)[1] if ":" in d else ""

def esc(x):
    return html.escape(str(x)) if x not in (None, "") else "—"

def highlight(text, names):
    out = html.escape(text)
    for name in sorted({n for n in names if n and len(n) > 3}, key=len, reverse=True):
        out = re.sub(re.escape(html.escape(name)), lambda m: f"<mark>{m.group(0)}</mark>", out)
    return out

def render_row(row, draft, entries):
    d = decision(row) or ""
    uid = decided_id(row)
    parts = [f'<section id="{esc(uid or row["text"])}"><h2>{esc(row["text"])}</h2>',
             f'<div class="meta">{esc(row["kind"])} · {esc(row["mentions"])} mentions · volumes {esc(row["volumes"])}'
             f' · decision <code>{esc(d or "undecided")}</code></div>']
    if row["subentries"]:
        parts.append(f'<div class="meta">Subentries: {esc(row["subentries"])}</div>')
    if row["cross_refs"]:
        parts.append(f'<div class="meta">Index cross-references: {esc(row["cross_refs"])}</div>')
    if d.startswith("new:"):
        if not draft:
            parts.append('<div class="card warn">No draft yet.</div>')
        else:
            desc = draft.get("description") or {}
            names = [draft.get("preferred_label", {}).get("value")] + [v.get("value") for v in draft.get("variant_labels", []) if isinstance(v, dict)]
            fields = {k: draft.get(k) for k in ("birth_date", "death_date", "start_date", "end_date", "location", "type") if k in draft}
            parts.append('<div class="card">')
            parts.append(f'<div class="label">Draft record</div><b>{esc(draft.get("preferred_label", {}).get("value"))}</b> '
                         f'<code>{esc(uid)}</code><br>Other names: {esc(", ".join(n for n in names[1:] if n))}')
            if desc.get("value"):
                parts.append(f'<p>{esc(desc["value"])}</p><div class="meta">Source: {esc(desc.get("source"))}</div>')
            else:
                parts.append('<p class="warn">No description: the sources were not enough. Needs research.</p>')
            if fields:
                parts.append("<div>" + " · ".join(f"{esc(k)}: {esc(v)}" for k, v in fields.items()) + "</div>")
            if draft.get("relationships"):
                parts.append("<div>Relationships: " + ", ".join(f"{esc(r.get('type'))} → <code>{esc(r.get('related_id'))}</code>" for r in draft["relationships"]) + "</div>")
            if draft.get("draft_notes"):
                parts.append(f'<div class="meta"><b>Draft notes:</b> {esc(draft["draft_notes"])}</div>')
            parts.append("</div>")
    sources, _ = evidence_for(entries, row)
    names = [row["text"].split(": ", 1)[0].split(",")[0], row["text"]]
    if draft:
        names += [draft.get("preferred_label", {}).get("value")] + [v.get("value") for v in draft.get("variant_labels", []) if isinstance(v, dict)]
    if sources:
        parts.append('<div class="label">Sources given to the draft</div>')
        for s in sources:
            parts.append(f'<div class="meta">{esc(s["label"])}</div><blockquote>{highlight(s["text"], names)}</blockquote>')
    else:
        parts.append('<div class="meta">No passages available.</div>')
    parts.append("</section>")
    return "\n".join(parts)

def write_page(batch):
    rows = [r for r in load_review() if r["batch"] == str(batch)]
    drafts = load_drafts()
    entries = load_entries()
    body = [render_row(r, drafts.get(decided_id(r)), entries) for r in rows]
    nav = " ".join(f'<a href="#{esc(decided_id(r) or r["text"])}">{esc(r["text"])}</a>' for r in rows)
    page = (f"<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Review batch {batch}</title><style>{CSS}</style>"
            f"<h1>Review batch {batch}</h1><div class='sub'>{len(rows)} rows. Decide in curated/review.csv; edit records in curated/drafts.jsonl.</div>"
            f"<nav>{nav}</nav>" + "\n".join(body))
    out = DATA_DIR / "review" / f"batch-{batch}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"Wrote {out} ({len(rows)} rows)")
    return out

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write a readable review page for a batch.")
    parser.add_argument("batch", type=int)
    write_page(parser.parse_args().batch)
