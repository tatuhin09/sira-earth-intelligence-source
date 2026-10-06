from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# SIRA_TEST_SRC_PATH_FIX_V1
SRC = ROOT / "src"
SRC_TEXT = str(SRC)
sys.path[:] = [SRC_TEXT] + [entry for entry in sys.path if entry != SRC_TEXT]

from sira.memory_integrity import (
    build_integrity_report,
    inspect_database,
    main,
)


def make_db(
    path: Path,
    *,
    schema_version: int = 2,
    memories: int = 2,
    occurrences: int = 3,
    transitions: int = 1,
) -> None:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
            (str(schema_version),),
        )
        conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE occurrences (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE transitions (id INTEGER PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO memories(id) VALUES (?)",
            [(i + 1,) for i in range(memories)],
        )
        conn.executemany(
            "INSERT INTO occurrences(id) VALUES (?)",
            [(i + 1,) for i in range(occurrences)],
        )
        conn.executemany(
            "INSERT INTO transitions(id) VALUES (?)",
            [(i + 1,) for i in range(transitions)],
        )
        conn.commit()


class MemoryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_healthy_database_reports_quick_check_schema_and_counts(self):
        db = self.root / "memory.db"
        make_db(db, schema_version=2, memories=4, occurrences=7, transitions=2)

        report = inspect_database(db)

        self.assertTrue(report.healthy)
        self.assertEqual(report.quick_check, "ok")
        self.assertEqual(report.schema_version, 2)
        self.assertEqual(report.counts["memories"], 4)
        self.assertEqual(report.counts["occurrences"], 7)
        self.assertEqual(report.counts["transitions"], 2)

    def test_missing_database_fails_closed_without_creating_file(self):
        db = self.root / "missing.db"

        report = inspect_database(db)

        self.assertFalse(report.healthy)
        self.assertEqual(report.errors, ("database_missing",))
        self.assertFalse(db.exists())

    def test_corrupt_database_is_unhealthy_and_error_is_redacted(self):
        db = self.root / "broken.db"
        db.write_bytes(b"not a sqlite database")

        report = inspect_database(db)

        self.assertFalse(report.healthy)
        self.assertTrue(
            any(error.startswith("database_error:") for error in report.errors)
        )
        self.assertNotIn("not a sqlite database", " ".join(report.errors))

    def test_matching_backup_is_migration_ready(self):
        primary = self.root / "primary.db"
        backup = self.root / "backup.db"
        make_db(primary, schema_version=2, memories=3, occurrences=8, transitions=2)
        make_db(backup, schema_version=2, memories=3, occurrences=8, transitions=2)

        report = build_integrity_report(primary, backup)

        self.assertTrue(report.primary.healthy)
        self.assertTrue(report.backup and report.backup.healthy)
        self.assertTrue(report.schema_match)
        self.assertTrue(report.parity)
        self.assertTrue(report.migration_ready)
        self.assertEqual(
            report.count_deltas,
            {"memories": 0, "occurrences": 0, "transitions": 0},
        )

    def test_count_divergence_blocks_parity_and_reports_delta(self):
        primary = self.root / "primary.db"
        backup = self.root / "backup.db"
        make_db(primary, memories=5, occurrences=8, transitions=2)
        make_db(backup, memories=3, occurrences=7, transitions=2)

        report = build_integrity_report(primary, backup)

        self.assertFalse(report.parity)
        self.assertFalse(report.migration_ready)
        self.assertEqual(report.count_deltas["memories"], 2)
        self.assertEqual(report.count_deltas["occurrences"], 1)
        self.assertEqual(report.count_deltas["transitions"], 0)

    def test_schema_mismatch_blocks_parity(self):
        primary = self.root / "primary.db"
        backup = self.root / "backup.db"
        make_db(primary, schema_version=2)
        make_db(backup, schema_version=1)

        report = build_integrity_report(primary, backup)

        self.assertFalse(report.schema_match)
        self.assertFalse(report.parity)
        self.assertFalse(report.migration_ready)

    def test_primary_only_health_can_be_used_as_nonparity_preflight(self):
        primary = self.root / "primary.db"
        make_db(primary)

        report = build_integrity_report(primary)

        self.assertTrue(report.primary.healthy)
        self.assertIsNone(report.backup)
        self.assertIsNone(report.parity)
        self.assertTrue(report.migration_ready)

    def test_cli_outputs_json_and_require_parity_uses_nonzero_exit(self):
        primary = self.root / "primary.db"
        backup = self.root / "backup.db"
        make_db(primary, memories=2)
        make_db(backup, memories=1)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = main(
                [
                    "--db",
                    str(primary),
                    "--backup",
                    str(backup),
                    "--require-parity",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(rc, 3)
        self.assertFalse(payload["parity"])
        self.assertEqual(payload["count_deltas"]["memories"], 1)


if __name__ == "__main__":
    unittest.main()
