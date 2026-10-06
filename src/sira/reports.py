"""Deterministic extractive evidence, escaped for Markdown and static HTML."""
import html
import re
from urllib.parse import quote as urlquote


def evidence_quotes(question, source_id, document):
    terms = set(re.findall(r"\w{3,}", question.casefold()))
    candidates = []
    for match in re.finditer(r"[^\r\n]+", document.text):
        raw = match.group()
        if not raw.strip():
            continue
        start = match.start() + len(raw) - len(raw.lstrip())
        end = min(start + 500, match.start() + len(raw.rstrip()))
        text = document.text[start:end]
        score = len(terms & set(re.findall(r"\w{3,}", text.casefold())))
        candidates.append((score, start, end, text))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    selected = [row for row in candidates if row[0] > 0][:2] or candidates[:1]
    return [{"source_id": source_id, "url": document.url, "start": start, "end": end,
             "quote": text, "content_sha256": document.content_sha256,
             "selection_method": "keyword_overlap_v1", "trust": "untrusted"}
            for score, start, end, text in selected]


def markdown_text(value):
    value = html.escape(str(value))
    return re.sub(r"([\\`*_{}\[\]()!#|>])", r"\\\1", value).replace("\r", " ").replace("\n", " ")


def _evidence_note(result):
    if result.get("evidence_origin") == "scholarly_paper":
        return ("Extractive quotations from untrusted scholarly paper abstracts or bounded "
                "open-access PDF text. Not a fact-checked answer.")
    return "Extractive quotations from untrusted provider-read text. Not a fact-checked answer."


def render_markdown(result):
    lines = ["# SIRA source evidence", "", markdown_text(result["question"]), "",
             _evidence_note(result), "",
             f"Run: {result['run_id']} | Parent: {result['parent_run_id']} | Status: {result['status']}", ""]
    for record in result["documents"]:
        sid = record["source_id"]
        lines.extend([f"## [{sid}] {markdown_text(record['title'])}", "",
                      f"Reading status: {record['status']}. Verification: {record['verification']}.", ""])
        if record["status"] == "read":
            url = urlquote(record["url"], safe=":/?&=%#@+,-._~")
            lines.extend([f"Source: [{sid}]({url})", "",
                          f"Retrieved: {record['retrieved_at']} | SHA-256: {record['content_sha256']}", ""])
        for item in result["evidence"]:
            if item["source_id"] == sid:
                lines.extend([f"> {markdown_text(item['quote'])} [{sid}]", ""])
    provenance_note = ("Quote integrity measures stored-text matching only. Paper evidence remains untrusted "
                       "until the synthesis verifier accepts a claim." if result.get("evidence_origin") == "scholarly_paper"
                       else "Quote integrity measures stored-text matching only. Search and reading use the same provider.")
    lines += ["## Evaluation", "", "Factual accuracy, semantic citation correctness and coverage: not evaluated.",
              provenance_note, ""]
    return "\n".join(lines)


def render_html(result):
    esc = html.escape
    cards = []
    for record in result["documents"]:
        sid = record["source_id"]
        source_link = (f'<a href="{esc(record["url"], quote=True)}" rel="noreferrer">Open source</a>'
                       if record["status"] == "read" else "Source not read")
        quotes = "".join(f'<blockquote>{esc(q["quote"])} <small>[{esc(sid)}]</small></blockquote>'
                         for q in result["evidence"] if q["source_id"] == sid)
        cards.append(f'<section><h2>[{esc(sid)}] {esc(record["title"])}</h2>'
                     f'<p>{esc(record["status"])} · {esc(record["verification"])}</p>'
                     f'{source_link}{quotes}</section>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>SIRA source evidence</title><style>'
            'body{font:17px/1.6 system-ui,sans-serif;background:#f5f6fa;color:#18213a;max-width:920px;margin:40px auto;padding:0 24px}'
            'section{background:white;padding:24px;margin:20px 0;border:1px solid #dde2ee;border-radius:12px}'
            'h1,h2{line-height:1.3}h2{font-size:21px}a{color:#4943b4}blockquote{border-left:3px solid #7772cd;padding-left:18px;margin:24px 0;white-space:pre-wrap;overflow-wrap:anywhere}'
            'small,.muted{color:#596275}header{margin-bottom:28px}</style>'
            f'<header><h1>SIRA source evidence</h1><p>{esc(result["question"])}</p>'
            f'<p class="muted">{esc(_evidence_note(result))} Extraction does not establish factual truth.</p>'
            f'<small>Run {result["run_id"]} · Status: {result["status"]}</small></header>'
            + "".join(cards) + '</html>')
