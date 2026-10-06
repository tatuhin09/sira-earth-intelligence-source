"""Local Library v1: safe scan, resumable FTS5 index, provenance-carrying search."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira import library
from sira.library import LibraryError

PYTEST = ("Pytest fixtures provide a fixed baseline so tests execute reliably and repeatably. "
          "A fixture is created by decorating a function and requested by name from a test. " * 2)
UNITTEST = ("The unittest framework supports test discovery, fixtures through setUp and tearDown, "
            "and aggregation of tests into suites. " * 3)


class LibraryBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo, self.lib = base / "repo", base / "biglib"
        self.repo.mkdir()
        self.lib.mkdir()
        library.init_library(self.repo, self.lib)
        self.col = self.lib / "collections"

    def put(self, relative, text, *, binary=False):
        path = self.col / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if binary else text.encode("utf-8"))
        return path

    def sync(self, **kwargs):
        return library.scan(self.repo), library.index(self.repo, **kwargs)


class ConfigTests(LibraryBase):
    def test_init_creates_layout_and_config_outside_the_repo(self):
        self.assertTrue((self.lib / "collections" / "README.txt").is_file())
        self.assertTrue((self.lib / "index").is_dir())
        config = library.load_config(self.repo)
        self.assertEqual(config["library_root"], self.lib.resolve())

    def test_unsafe_library_paths_are_rejected(self):
        for bad in (Path("relative/path"), Path("/"), self.repo, self.repo / "src",
                    Path(self.temp.name)):  # parent of the repo contains it
            with self.subTest(path=str(bad)), self.assertRaises(LibraryError):
                library.init_library(self.repo, bad)
        with self.assertRaises(LibraryError):
            library.init_library(self.repo, self.lib, max_file_bytes=10)

    def test_malformed_or_missing_config_fails_closed(self):
        path = self.repo / "memory/library/config.json"
        path.write_text("{bad")
        with self.assertRaises(LibraryError):
            library.load_config(self.repo)
        path.unlink()
        with self.assertRaises(LibraryError):
            library.scan(self.repo)

    def test_trust_classes_are_owner_set_and_default_to_unknown(self):
        self.put("python-docs/pytest/a.md", PYTEST)
        self.assertEqual(library.collection_trust(self.lib, "python-docs"), "unknown")
        library.set_trust(self.repo, "python-docs", "official_documentation", "pytest docs")
        self.assertEqual(library.collection_trust(self.lib, "python-docs"), "official_documentation")
        with self.assertRaises(LibraryError):
            library.set_trust(self.repo, "python-docs", "trust_me")
        with self.assertRaises(LibraryError):
            library.set_trust(self.repo, "no-such-collection", "reference_book")
        with self.assertRaises(LibraryError):
            library.set_trust(self.repo, "../escape", "reference_book")


class ScanIndexTests(LibraryBase):
    def test_scan_and_index_supported_formats_with_source_independence(self):
        self.put("py/pytest-docs/fixtures.md", PYTEST)
        self.put("py/unittest-docs/index.html",
                 f"<html><head><title>x</title><style>p{{}}</style></head><body><nav>menu</nav>"
                 f"<main><p>{UNITTEST}</p></main><script>var a=1</script></body></html>")
        self.put("py/top-level.txt", UNITTEST)
        self.put("py/pytest-docs/image.png", "not text")
        scan, indexed = self.sync()
        self.assertEqual((scan["new"], scan["ignored_unsupported"]), (3, 1))
        self.assertEqual((indexed["indexed"], indexed["status"]), (3, "completed"))
        status = library.status(self.repo)
        self.assertEqual(status["files_by_status"], {"indexed": 3})
        db = sqlite3.connect(self.lib / "index/library.sqlite3")
        sources = {r[0] for r in db.execute("SELECT source FROM files")}
        self.assertEqual(sources, {"pytest-docs", "unittest-docs", "_root"})

    def test_html_text_drops_scripts_styles_and_markup(self):
        text = library.extract_text(
            b"<html><head><title>T</title></head><body><script>evil()</script><style>a{}</style>"
            b"<h1>Heading</h1><p>Body &amp; more text here for the extractor to keep.</p></body></html>",
            ".html")
        self.assertIn("Heading", text)
        self.assertIn("Body & more", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("a{}", text)

    def test_unchanged_files_are_not_reindexed_and_changed_files_are(self):
        path = self.put("c/s/a.md", PYTEST)
        self.sync()
        scan, indexed = self.sync()
        self.assertEqual((scan["unchanged"], indexed["indexed"]), (1, 0))
        path.write_text(PYTEST + " Brand new sentence about parametrization of fixtures.")
        os.utime(path, ns=(1, 2_000_000_000))
        scan, indexed = self.sync()
        self.assertEqual((scan["changed"], indexed["indexed"]), (1, 1))
        hits = library.search(self.repo, "parametrization")
        self.assertEqual(len(hits), 1)

    def test_deleted_files_leave_no_chunks_behind(self):
        path = self.put("c/s/a.md", PYTEST)
        self.sync()
        self.assertTrue(library.search(self.repo, "fixtures"))
        path.unlink()
        scan, _ = self.sync()
        self.assertEqual(scan["removed"], 1)
        self.assertEqual(library.search(self.repo, "fixtures"), [])
        db = sqlite3.connect(self.lib / "index/library.sqlite3")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 0)

    def test_secrets_binary_oversized_and_empty_files_are_skipped_not_indexed(self):
        self.put("c/s/key.md", "Notes\nAPI_KEY = 'AIza" + "A" * 36 + "'\nmore text " * 5)
        self.put("c/s/binary.txt", b"abc\x00\x01\x02" * 50, binary=True)
        self.put("c/s/empty.md", "tiny")
        self.put("c/s/big.md", "word " * 400)
        self.put("c/s/ok.md", PYTEST)
        library.scan(self.repo)
        config = library.load_config(self.repo)
        with patch.object(library, "load_config", return_value={**config, "max_file_bytes": 1000}):
            result = library.index(self.repo)
        self.assertEqual(result["indexed"], 1)
        reasons = library.status(self.repo)["skipped_reasons"]
        self.assertEqual(set(reasons), {"secret_like_content", "binary_like", "empty", "oversized"})
        self.assertEqual(library.search(self.repo, "AIza"), [])

    def test_symlinks_hidden_entries_and_escapes_are_never_followed(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text(PYTEST)
        self.put("c/s/ok.md", PYTEST)
        os.symlink(outside, self.col / "c" / "s" / "linked-dir")
        os.symlink(outside / "secret.md", self.col / "c" / "s" / "linked-file.md")
        self.put("c/.hidden/x.md", PYTEST)
        scan, _ = self.sync()
        self.assertEqual(scan["new"], 1)
        self.assertEqual({h["relpath"] for h in library.search(self.repo, "fixtures", limit=20)},
                         {"collections/c/s/ok.md"})

    def test_indexing_is_resumable_and_bounded(self):
        for index in range(7):
            self.put(f"c/s/f{index}.md", PYTEST + f" unique{index}token.")
        library.scan(self.repo)
        first = library.index(self.repo, max_files=3)
        self.assertEqual((first["indexed"], first["remaining"]), (3, 4))
        second = library.index(self.repo, max_files=10)
        self.assertEqual((second["indexed"], second["remaining"]), (4, 0))
        slow = library.index(self.repo, max_files=10)
        self.assertEqual(slow["indexed"], 0)

    def test_time_limit_stops_cleanly_and_low_disk_refuses(self):
        for index in range(3):
            self.put(f"c/s/f{index}.md", PYTEST)
        library.scan(self.repo)
        ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0, 5.0])
        with patch.object(library.time, "monotonic", side_effect=lambda: next(ticks)):
            result = library.index(self.repo, max_files=10, seconds=1)
        self.assertEqual(result["status"], "time_limit_reached")
        with patch.object(library, "_free_bytes", return_value=1024):
            self.assertEqual(library.index(self.repo)["status"], "insufficient_disk_space")

    def test_chunks_are_bounded_and_keep_offsets(self):
        text = "\n\n".join(f"Paragraph {i} " + "content words " * 30 for i in range(12))
        text += "\n\n" + "x" * 5000
        chunks = library.chunk_text(text)
        self.assertTrue(all(20 <= len(c) <= library.CHUNK_MAX for _s, c in chunks))
        self.assertEqual(chunks[0][0], 0)
        starts = [s for s, _c in chunks]
        self.assertEqual(starts, sorted(starts))


class SearchTests(LibraryBase):
    def setUp(self):
        super().setUp()
        self.put("docs/pytest/fixtures.md", PYTEST)
        self.put("docs/unittest/intro.md", UNITTEST)
        self.put("notes/me/todo.md", "My private reminder: buy bananas and call the plumber tomorrow morning.")
        library.set_trust(self.repo, "docs", "official_documentation")
        self.sync()

    def test_search_ranks_and_carries_full_provenance(self):
        hits = library.search(self.repo, "pytest fixtures reliably")
        self.assertEqual(hits[0]["source"], "pytest")
        self.assertEqual(hits[0]["relpath"], "collections/docs/pytest/fixtures.md")
        raw = (self.col / "docs/pytest/fixtures.md").read_bytes()
        self.assertEqual(hits[0]["file_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(hits[0]["trust"], "official_documentation")
        self.assertTrue(hits[0]["evidence_eligible"])
        self.assertLessEqual(len(hits[0]["text"]), library.SNIPPET_CHARS)

    def test_stemming_collection_filter_and_evidence_only(self):
        self.assertTrue(library.search(self.repo, "fixture"))  # matches "fixtures"
        self.assertEqual({h["collection"] for h in library.search(self.repo, "reminder bananas")}, {"notes"})
        self.assertEqual(library.search(self.repo, "bananas", evidence_only=True), [])
        self.assertEqual(library.search(self.repo, "fixtures", collection="notes"), [])
        notes = library.search(self.repo, "bananas")[0]
        self.assertEqual((notes["trust"], notes["evidence_eligible"]), ("unknown", False))

    def test_query_text_cannot_inject_fts_syntax(self):
        for query in ('"unbalanced', "fixtures OR NEAR(", "col:evil *", "fix* -tests", "'; DROP TABLE files;--",
                      "a b"):
            with self.subTest(query=query):
                try:
                    library.search(self.repo, query)
                except LibraryError:
                    pass  # no searchable words is a clean error, never a SQL error
        db = sqlite3.connect(self.lib / "index/library.sqlite3")
        self.assertGreater(db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_or_fallback_finds_partial_matches_and_bad_input_is_rejected(self):
        self.assertTrue(library.search(self.repo, "pytest nonexistentwordzzz"))
        for query in ("", "x", "!!! ???", "y" * 301):
            with self.subTest(query=query), self.assertRaises(LibraryError):
                library.search(self.repo, query)
        with self.assertRaises(LibraryError):
            library.search(self.repo, "pytest", collection="../x")

    def test_status_reports_disk_and_collections_and_verify_detects_tampering(self):
        status = library.status(self.repo)
        self.assertEqual({c["collection"] for c in status["collections"]}, {"docs", "notes"})
        self.assertGreater(status["disk_free_bytes"], 0)
        self.assertTrue(library.verify_sample(self.repo, sample=10, seed=1)["ok"])
        (self.col / "docs/pytest/fixtures.md").write_text("tampered content here " * 10)
        report = library.verify_sample(self.repo, sample=10, seed=1)
        self.assertFalse(report["ok"])
        self.assertIn("collections/docs/pytest/fixtures.md", report["changed"])


class SafetyTests(unittest.TestCase):
    def test_module_has_no_network_model_or_execution_capability(self):
        source = (ROOT / "src/sira/library.py").read_text()
        for forbidden in ("import socket", "urllib", "import requests", "http.client",
                          "subprocess", "os.system", "exec(", "eval(", "__import__"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
