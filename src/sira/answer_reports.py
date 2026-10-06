"""Deterministic answer rendering from claims already accepted by the local gate."""
import html
import re


def _markdown_text(value):
    """Make untrusted text display literally in common Markdown renderers."""
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>])", r"\\\1", escaped)


def answer_markdown(result):
    lines = ["# SIRA verified evidence answer", ""]
    if result["accepted_claims"]:
        for item in result["accepted_claims"]:
            citations = " ".join(f"[{source_id}]" for source_id in item["source_ids"])
            lines.extend([f"- {_markdown_text(item['text'])} {citations}", ""])
    else:
        lines.extend(["Insufficient verified evidence to answer this question.", ""])
    lines.extend(["## Sources", ""])
    for source in result["sources"]:
        lines.append(f"- [{source['source_id']}] {_markdown_text(source['title'])} — <{source['url']}>")
    lines.extend(["", "## Verification boundary", "",
                  "Claims passed a same-model second review and local citation checks. "
                  "This is not an independent final evaluation or a measurement of factual accuracy.", ""])
    return "\n".join(lines)


def answer_html(result):
    claims = "".join(
        "<li>" + html.escape(item["text"]) + " "
        + " ".join(f"<strong>[{html.escape(s)}]</strong>" for s in item["source_ids"]) + "</li>"
        for item in result["accepted_claims"])
    if not claims:
        claims = "<li>Insufficient verified evidence to answer this question.</li>"
    sources = "".join(
        f'<li><strong>[{html.escape(s["source_id"])}]</strong> '
        f'{html.escape(s["title"])} — <a rel="noreferrer" href="{html.escape(s["url"], quote=True)}">'
        f'{html.escape(s["url"])}</a></li>' for s in result["sources"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>SIRA verified evidence answer</title>
<style>body{{font:16px/1.55 system-ui;max-width:850px;margin:2rem auto;padding:0 1rem}}li{{margin:.7rem 0}}code{{background:#eee;padding:.1rem .25rem}}</style>
</head><body><h1>SIRA verified evidence answer</h1><ul>{claims}</ul>
<h2>Sources</h2><ul>{sources}</ul>
<h2>Verification boundary</h2><p>Claims passed a same-model second review and local citation checks.
This is not an independent final evaluation or a measurement of factual accuracy.</p></body></html>"""
