"""Bounded text extraction shared by SIRA's PDF readers.

Text based PDFs use Poppler's ``pdftotext`` when available. A small,
dependency free parser remains as a fallback for simple public PDFs. Image-only
documents need OCR and are rejected explicitly rather than treated as blank evidence.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import zlib
from pathlib import Path


DEFAULT_MAX_PDF_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 8_000_000
DEFAULT_TIMEOUT_SECONDS = 20
MAX_PAGES = 500
MAX_SIMPLE_STREAM_BYTES = 2 * 1024 * 1024

_STREAM = re.compile(rb"(?P<dict><<.{0,4096}?>>)?\s*stream\r?\n(?P<data>.*?)\r?\nendstream", re.DOTALL)
_BT = re.compile(rb"BT(.*?)ET", re.DOTALL)
_LITERAL = re.compile(rb"\((?:\\.|[^\\()])*\)")
_HEX = re.compile(rb"<([0-9A-Fa-f\s]+)>")


class PdfExtractionError(ValueError):
    """A stable, safe-to-record reason why a PDF could not be read."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _decode_bytes(data: bytes) -> str:
    if data.startswith((b"\xfe\xff", b"\xff\xfe")):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    return ""


def _decode_literal(token: bytes) -> str:
    data = token[1:-1]
    out = bytearray()
    i = 0
    while i < len(data):
        value = data[i]
        if value != 0x5C:
            out.append(value)
            i += 1
            continue
        i += 1
        if i >= len(data):
            break
        value = data[i]
        escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8,
                   ord("f"): 12, ord("("): 40, ord(")"): 41, ord("\\"): 92}
        if value in escapes:
            out.append(escapes[value])
            i += 1
            continue
        if 48 <= value <= 55:
            digits = bytes([value])
            i += 1
            for _ in range(2):
                if i < len(data) and 48 <= data[i] <= 55:
                    digits += bytes([data[i]])
                    i += 1
                else:
                    break
            out.append(int(digits, 8))
            continue
        if value == 13:
            i += 1
            if i < len(data) and data[i] == 10:
                i += 1
            continue
        if value == 10:
            i += 1
            continue
        out.append(value)
        i += 1
    return _decode_bytes(bytes(out))


def _decode_hex(token: bytes) -> str:
    raw = re.sub(rb"\s+", b"", token)
    if len(raw) % 2:
        raw += b"0"
    try:
        return _decode_bytes(bytes.fromhex(raw.decode("ascii")))
    except (ValueError, UnicodeError):
        return ""


def _clean_page(text: str) -> str:
    text = "".join(ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    printable = sum(ch.isprintable() or ch in "\n\t" for ch in text)
    if text and printable / max(1, len(text)) < 0.90:
        raise PdfExtractionError("pdf_unreadable_text")
    return text


def _format_pages(raw_text: str, max_chars: int) -> str:
    pages = []
    for number, page in enumerate(raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\f"), 1):
        cleaned = _clean_page(page)
        if cleaned:
            pages.append(f"[Page {number}]\n\n{cleaned}")
    if not pages:
        raise PdfExtractionError("pdf_no_text")
    text = "\n\n".join(pages)
    if len(text) > max_chars:
        raise PdfExtractionError("pdf_text_too_large")
    return text


def _extract_simple_pdf_text(data: bytes, max_chars: int) -> str:
    """Small fallback parser for uncompressed and Flate text streams."""
    parts: list[str] = []
    total_chars = 0
    for match in _STREAM.finditer(data):
        dictionary = match.group("dict") or b""
        stream = match.group("data")
        if len(stream) > MAX_SIMPLE_STREAM_BYTES:
            continue
        if b"/FlateDecode" in dictionary:
            try:
                inflater = zlib.decompressobj()
                stream = inflater.decompress(stream, MAX_SIMPLE_STREAM_BYTES + 1)
                if len(stream) > MAX_SIMPLE_STREAM_BYTES or inflater.unconsumed_tail:
                    continue
                stream += inflater.flush(MAX_SIMPLE_STREAM_BYTES + 1 - len(stream))
                if len(stream) > MAX_SIMPLE_STREAM_BYTES:
                    continue
            except (zlib.error, TypeError, ValueError):
                continue
        elif re.search(rb"/Filter\s*/(?!FlateDecode)", dictionary):
            continue
        for block in _BT.findall(stream):
            for token in _LITERAL.findall(block):
                value = _decode_literal(token)
                if value.strip():
                    parts.append(value)
                    total_chars += len(value)
            for token in _HEX.findall(block):
                value = _decode_hex(token)
                if value.strip():
                    parts.append(value)
                    total_chars += len(value)
            if parts:
                parts.append("\n")
        if total_chars > max_chars:
            raise PdfExtractionError("pdf_text_too_large")
    return " ".join(parts)


def _run_pdftotext(data: bytes, *, timeout: int, max_output_bytes: int) -> bytes:
    executable = shutil.which("pdftotext")
    if not executable:
        raise PdfExtractionError("pdf_extractor_unavailable")
    try:
        with tempfile.TemporaryDirectory(prefix="sira-pdf-") as folder:
            source = Path(folder) / "source.pdf"
            source.write_bytes(data)
            process = subprocess.Popen(
                [executable, "-enc", "UTF-8", "-layout", "-f", "1", "-l",
                 str(MAX_PAGES), str(source), "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            output = bytearray()
            exceeded = []

            def drain_output():
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    remaining = max_output_bytes + 1 - len(output)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    if len(output) > max_output_bytes:
                        exceeded.append(True)
                        try:
                            process.kill()
                        except OSError:
                            pass
                        break

            reader = threading.Thread(target=drain_output, name="sira-pdf-output", daemon=True)
            reader.start()
            try:
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise PdfExtractionError("pdf_timeout") from exc
            finally:
                reader.join()
                if process.stdout is not None:
                    process.stdout.close()
            if exceeded:
                raise PdfExtractionError("pdf_text_too_large")
            if return_code != 0:
                raise PdfExtractionError("pdf_parse_failed")
            return bytes(output)
    except PdfExtractionError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise PdfExtractionError("pdf_parse_failed") from None


def extract_pdf_text(data: bytes, *, max_bytes: int = DEFAULT_MAX_PDF_BYTES,
                     max_chars: int = DEFAULT_MAX_TEXT_CHARS,
                     timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Extract bounded, page-labelled text from a public or owner-provided PDF."""
    if not isinstance(data, bytes) or not data.startswith(b"%PDF-"):
        raise PdfExtractionError("invalid_pdf")
    if len(data) > max_bytes:
        raise PdfExtractionError("pdf_too_large")
    if b"/Encrypt" in data[-2 * 1024 * 1024:]:
        raise PdfExtractionError("encrypted_pdf")
    if type(max_chars) is not int or max_chars < 1 or type(timeout) is not int or timeout < 1:
        raise ValueError("Invalid PDF extraction limits")
    max_output_bytes = max_chars * 4
    try:
        raw_text = _run_pdftotext(data, timeout=timeout, max_output_bytes=max_output_bytes)
        text = raw_text.decode("utf-8", errors="replace")
    except PdfExtractionError as exc:
        if exc.code in {"pdf_timeout", "pdf_text_too_large"}:
            raise
        text = _extract_simple_pdf_text(data, max_chars)
    if not text.strip():
        text = _extract_simple_pdf_text(data, max_chars)
    return _format_pages(text, max_chars)
