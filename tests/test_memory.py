import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys
from contextlib import closing, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sira.cli import main
from sira.memory import (MemoryStore, Observation, learn_all, learn_run,
                         memory_quality_score)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_run(self, run_id, name, data):
        path = self.root / "runs" / run_id
        path.mkdir()
        data = dict(data)
        data.setdefault("run_id", run_id)
        (path / name).write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_repeated_rate_limit_is_deduplicated_but_occurrences_are_preserved(self):
        for run_id in ("a" * 32, "b" * 32):
            self._write_run(run_id, "papers.json", {
                "schema_version": 1,
                "status": "failed",
                "provider": "semantic_scholar",
                "error": {"code": "rate_limited", "retry_after_seconds": None},
                "metrics": {"api_requests": 1, "failures": 1},
                "papers": [],
            })
            result = learn_run(self.root, run_id)
            self.assertEqual(result["observations_created"], 1)

        store = MemoryStore(self.root)
        rows = store.search("rate limit")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "rate_limit")
        self.assertEqual(rows[0]["capability"], "scholarly_research")
        self.assertEqual(rows[0]["occurrence_count"], 2)
        detail = store.show(rows[0]["memory_id"])
        self.assertEqual({o["run_id"] for o in detail["occurrences"]}, {"a" * 32, "b" * 32})

    def test_completed_answer_and_rejected_claim_become_separate_memories(self):
        run_id = "c" * 32
        self._write_run(run_id, "answer.json", {
            "schema_version": 1,
            "kind": "verified_evidence_answer",
            "status": "completed",
            "error": None,
            "model": {"provider": "gemini", "id": "fixture"},
            "accepted_claims": [{"id": "C1", "text": "Supported"}],
            "rejected_claims": [{
                "id": "C2", "text": "Unsupported statement",
                "reasons": ["verdict_unsupported", "unknown_citation"],
            }],
            "metrics": {"accepted_claims": 1, "rejected_claims": 1},
        })
        result = learn_run(self.root, run_id)
        self.assertEqual(result["observations_created"], 2)
        store = MemoryStore(self.root)
        stats = store.stats()
        self.assertEqual(stats["total_memories"], 2)
        self.assertEqual(stats["by_kind"]["success"], 1)
        self.assertEqual(stats["by_kind"]["rejection"], 1)
        success = store.search("synthesis completed")[0]
        self.assertEqual(success["status"], "validated")
        rejected = store.search("Unsupported statement")[0]
        self.assertEqual(rejected["status"], "observed")
        self.assertEqual(rejected["category"], "citation")

    def test_status_history_is_append_only_and_old_lesson_is_superseded(self):
        run_id = "d" * 32
        self._write_run(run_id, "result.json", {
            "schema_version": 1,
            "status": "failed", "provider": "tavily",
            "error": {"code": "transport_error"}, "metrics": {"failures": 1}, "sources": [],
        })
        learn_run(self.root, run_id)
        store = MemoryStore(self.root)
        memory_id = store.search("transport")[0]["memory_id"]
        store.transition(memory_id, "researched", "Investigated provider transport", run_id)
        store.transition(memory_id, "superseded", "A newer validated lesson replaces this one", run_id)
        detail = store.show(memory_id)
        self.assertEqual(detail["status"], "superseded")
        self.assertEqual([x["to_status"] for x in detail["transitions"]], ["researched", "superseded"])

    def test_database_uses_wal_and_recovers_from_latest_backup(self):
        run_id = "e" * 32
        self._write_run(run_id, "result.json", {
            "schema_version": 1, "status": "completed", "provider": "fixture",
            "error": None, "metrics": {"failures": 0}, "sources": [{"title": "x"}],
        })
        learn_run(self.root, run_id)
        store = MemoryStore(self.root)
        db_path = store.db_path
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertTrue(store.backup_path.is_file())
        db_path.write_bytes(b"not a sqlite database")
        recovered = MemoryStore(self.root)
        self.assertEqual(recovered.stats()["total_memories"], 1)
        quarantined = list((self.root / "memory" / "corrupt").glob("*.sqlite3"))
        self.assertEqual(len(quarantined), 1)

    def test_cli_learn_search_show_stats_and_learn_all_are_json(self):
        run_id = "f" * 32
        self._write_run(run_id, "result.json", {
            "schema_version": 1, "status": "failed", "provider": "fixture",
            "error": {"code": "timeout"}, "metrics": {"failures": 1}, "sources": [],
        })
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--root", str(self.root), "learn", run_id]), 0)
        learned = json.loads(out.getvalue())
        self.assertEqual(learned["status"], "learned")
        self.assertEqual(learned["memories_touched"], 1)

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--root", str(self.root), "memory", "search", "timeout"]), 0)
        rows = json.loads(out.getvalue())["results"]
        self.assertEqual(len(rows), 1)
        memory_id = rows[0]["memory_id"]

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--root", str(self.root), "memory", "show", memory_id]), 0)
        self.assertEqual(json.loads(out.getvalue())["memory_id"], memory_id)

        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--root", str(self.root), "memory", "stats"]), 0)
        self.assertEqual(json.loads(out.getvalue())["total_memories"], 1)

        second = "1" * 32
        self._write_run(second, "result.json", {
            "schema_version": 1, "status": "completed", "provider": "fixture",
            "error": None, "metrics": {"failures": 0}, "sources": [{"title": "ok"}],
        })
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--root", str(self.root), "learn", "--all"]), 0)
        self.assertGreaterEqual(json.loads(out.getvalue())["runs_learned"], 1)

    def test_learn_all_silently_skips_non_run_directories_but_explicit_learn_stays_strict(self):
        run_id = "2" * 32
        self._write_run(run_id, "result.json", {
            "schema_version": 1, "status": "completed", "provider": "fixture",
            "error": None, "metrics": {"failures": 0}, "sources": [{"title": "ok"}],
        })
        backup_dir = self.root / "runs" / "update_backup_deadbeef"
        backup_dir.mkdir()
        (backup_dir / "README.txt").write_text("not a SIRA run", encoding="utf-8")
        unrelated_dir = self.root / "runs" / "scratch_notes"
        unrelated_dir.mkdir()
        (unrelated_dir / "notes.txt").write_text("also not a SIRA run", encoding="utf-8")

        first = learn_all(self.root)
        self.assertEqual(first["errors"], [])
        self.assertEqual(first["runs_skipped"], 2)
        self.assertEqual(first["occurrences_added"], 1)

        second = learn_all(self.root)
        self.assertEqual(second["errors"], [])
        self.assertEqual(second["runs_skipped"], 2)
        self.assertEqual(second["occurrences_added"], 0)

        with self.assertRaisesRegex(ValueError, "No supported run artifact found"):
            learn_run(self.root, "update_backup_deadbeef")
        with self.assertRaisesRegex(ValueError, "No supported run artifact found"):
            learn_run(self.root, "scratch_notes")

    def test_path_traversal_and_unknown_run_are_rejected(self):
        with self.assertRaises(ValueError):
            learn_run(self.root, "../escape")
        with self.assertRaises(ValueError):
            learn_run(self.root, "0" * 32)

    def test_schema_v1_migrates_additively_and_preserves_history(self):
        memory_dir = self.root / "memory"
        memory_dir.mkdir()
        db = memory_dir / "sira_memory.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE memories (
                    memory_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    provider TEXT,
                    error_code TEXT,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL
                );
                CREATE TABLE occurrences (
                    occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    artifact_name TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    outcome_status TEXT,
                    observed_at TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    UNIQUE(memory_id, run_id, artifact_sha256)
                );
                CREATE TABLE transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    run_id TEXT,
                    changed_at TEXT NOT NULL
                );
            """)
            conn.execute("INSERT INTO metadata(key,value) VALUES('schema_version','1')")
            memory_id = "m_" + "9" * 32
            now = "2026-09-01T00:00:00+00:00"
            conn.execute(
                """INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, "f" * 64, "failure", "network", "retrieval", "tavily",
                 "timeout", "tavily timeout failure in retrieval", "observed", now, now, 1),
            )
            conn.execute(
                """INSERT INTO occurrences(memory_id,run_id,artifact_name,artifact_sha256,
                   outcome_status,observed_at,details_json) VALUES(?,?,?,?,?,?,?)""",
                (memory_id, "9" * 32, "result.json", "a" * 64, "failed", now, "{}"),
            )
            conn.commit()

        store = MemoryStore(self.root)
        self.assertEqual(store.stats()["schema_version"], 4)
        detail = store.show(memory_id)
        self.assertEqual(detail["occurrence_count"], 1)
        self.assertEqual(detail["real_occurrence_count"], 1)
        self.assertEqual(detail["synthetic_occurrence_count"], 0)
        self.assertEqual(detail["origin_kind"], "run_artifact")
        self.assertGreater(detail["confidence"], 0.0)
        self.assertEqual(len(detail["occurrences"]), 1)

    def test_quality_score_rewards_validation_and_freshness_and_penalizes_contradiction(self):
        validated = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2,
            contradiction_count=0, source_diversity=1, age_days=5,
        )
        observed = memory_quality_score(
            status="observed", occurrence_count=2, real_occurrence_count=2,
            contradiction_count=0, source_diversity=1, age_days=5,
        )
        stale = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2,
            contradiction_count=0, source_diversity=1, age_days=500,
        )
        contradicted = memory_quality_score(
            status="validated", occurrence_count=2, real_occurrence_count=2,
            contradiction_count=2, source_diversity=1, age_days=5,
        )
        self.assertGreater(validated, observed)
        self.assertGreater(validated, stale)
        self.assertGreater(validated, contradicted)
        self.assertTrue(all(0.0 <= x <= 1.0 for x in (validated, observed, stale, contradicted)))

    def test_synthetic_fixture_memory_is_auditable_but_not_an_autonomous_candidate(self):
        run_id = "7" * 32
        self._write_run(run_id, "result.json", {
            "schema_version": 1, "status": "failed", "provider": "fixture",
            "error": {"code": "timeout"}, "metrics": {"failures": 1}, "sources": [],
        })
        learn_run(self.root, run_id)
        store = MemoryStore(self.root)
        row = store.search("fixture timeout")[0]
        self.assertEqual(row["real_occurrence_count"], 0)
        self.assertEqual(row["synthetic_occurrence_count"], 1)
        self.assertNotIn(row["memory_id"], {x["memory_id"] for x in store.improvement_candidates(20)})

    def test_machine_memory_namespace_rejects_protected_owner_preferences(self):
        with self.assertRaisesRegex(ValueError, "protected owner/user preference"):
            Observation(
                "success", "success", "retrieval", "Owner prefers a private setting", "validated",
                provider="owner", origin="owner_preference",
            )

    def test_supersession_links_replacement_without_deleting_old_history(self):
        store = MemoryStore(self.root)
        old_id, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval", "old transport lesson", "observed",
                provider="tavily", error_code="transport_error", signature="old-lineage",
            ),
            run_id="5" * 32, artifact_name="result.json", artifact_sha256="5" * 64,
            outcome_status="failed",
        )
        new_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval", "new validated transport lesson", "validated",
                provider="tavily", signature="new-lineage",
            ),
            run_id="6" * 32, artifact_name="result.json", artifact_sha256="6" * 64,
            outcome_status="completed",
        )
        store.supersede(old_id, new_id, "newer validated lesson", "6" * 32)
        old = store.show(old_id)
        new = store.show(new_id)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by_memory_id"], new_id)
        self.assertEqual(new["supersedes_memory_id"], old_id)
        self.assertGreaterEqual(new["lineage_version"], 2)
        self.assertEqual(old["occurrence_count"], 1)
        self.assertEqual([x["to_status"] for x in old["transitions"]][-1], "superseded")


    def test_near_duplicate_real_memories_cluster_without_merging(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation(
                "failure", "rate_limit", "scholarly_research",
                "Semantic Scholar rate limited request", "observed",
                provider="semantic_scholar", error_code="rate_limited",
                signature="semantic scholar request throttled",
            ),
            run_id="a3" * 16, artifact_name="papers.json",
            artifact_sha256="3" * 64, outcome_status="failed",
        )
        b_id, _ = store.upsert(
            Observation(
                "failure", "rate_limit", "scholarly_research",
                "Semantic Scholar returned HTTP 429", "observed",
                provider="semantic_scholar", error_code="http_429",
                signature="semantic scholar request http 429",
            ),
            run_id="a4" * 16, artifact_name="papers.json",
            artifact_sha256="4" * 64, outcome_status="failed",
        )
        self.assertNotEqual(a_id, b_id)
        a_cluster = store.cluster(a_id)
        b_cluster = store.cluster(b_id)
        self.assertEqual(a_cluster["cluster_key"], b_cluster["cluster_key"])
        self.assertEqual({x["memory_id"] for x in a_cluster["members"]}, {a_id, b_id})
        self.assertEqual(len(a_cluster["members"]), 2)

    def test_unrelated_context_does_not_cluster(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval",
                "Tavily transport timeout", "observed",
                provider="tavily", error_code="timeout", signature="retrieval transport timeout",
            ),
            run_id="b3" * 16, artifact_name="result.json",
            artifact_sha256="5" * 64, outcome_status="failed",
        )
        b_id, _ = store.upsert(
            Observation(
                "success", "success", "scholarly_research",
                "Semantic Scholar completed successfully", "validated",
                provider="semantic_scholar", signature="scholarly completed",
            ),
            run_id="b4" * 16, artifact_name="papers.json",
            artifact_sha256="6" * 64, outcome_status="completed",
        )
        self.assertNotEqual(store.cluster(a_id)["cluster_key"], store.cluster(b_id)["cluster_key"])

    def test_exact_fingerprint_dedup_still_precedes_clustering(self):
        store = MemoryStore(self.root)
        obs = Observation(
            "failure", "network", "retrieval", "Repeated timeout", "observed",
            provider="tavily", error_code="timeout", signature="same-exact-memory",
        )
        a_id, _ = store.upsert(
            obs, run_id="c3" * 16, artifact_name="result.json",
            artifact_sha256="7" * 64, outcome_status="failed",
        )
        b_id, _ = store.upsert(
            obs, run_id="c4" * 16, artifact_name="result.json",
            artifact_sha256="8" * 64, outcome_status="failed",
        )
        self.assertEqual(a_id, b_id)
        self.assertEqual(store.show(a_id)["occurrence_count"], 2)

    def test_compatible_opposing_real_outcomes_create_contradiction_relation(self):
        store = MemoryStore(self.root)
        fail_id, _ = store.upsert(
            Observation(
                "failure", "rate_limit", "scholarly_research",
                "Semantic Scholar requests repeatedly rate limited", "observed",
                provider="semantic_scholar", error_code="rate_limited",
                signature="semantic scholar repeated request pattern",
            ),
            run_id="d3" * 16, artifact_name="papers.json",
            artifact_sha256="9" * 64, outcome_status="failed",
        )
        success_id, _ = store.upsert(
            Observation(
                "success", "success", "scholarly_research",
                "Semantic Scholar same request pattern completed successfully", "validated",
                provider="semantic_scholar",
                signature="semantic scholar repeated request pattern success",
            ),
            run_id="d4" * 16, artifact_name="papers.json",
            artifact_sha256="a" * 64, outcome_status="completed",
        )
        candidates = store.reconcile_candidate(success_id)
        contradiction = next(
            x for x in candidates
            if x["other_memory_id"] == fail_id and x["relation_type"] == "contradicts"
        )
        store.relate(
            fail_id, success_id, "contradicts",
            contradiction["confidence"], contradiction["reason"],
            evidence={"source": "deterministic_reconciliation"},
        )
        relations = store.relations(fail_id)
        self.assertTrue(any(
            x["relation_type"] == "contradicts"
            and x["other_memory_id"] == success_id for x in relations
        ))

    def test_reverse_relation_is_idempotent(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation("failure", "network", "retrieval", "network failure A", "observed",
                        provider="tavily", error_code="timeout", signature="rel-a"),
            run_id="e3" * 16, artifact_name="result.json",
            artifact_sha256="b" * 64, outcome_status="failed",
        )
        b_id, _ = store.upsert(
            Observation("failure", "network", "retrieval", "network failure B", "observed",
                        provider="tavily", error_code="transport_error", signature="rel-b"),
            run_id="e4" * 16, artifact_name="result.json",
            artifact_sha256="c" * 64, outcome_status="failed",
        )
        first = store.relate(a_id, b_id, "similar", 0.8, "same bounded context")
        second = store.relate(b_id, a_id, "similar", 0.8, "same bounded context")
        self.assertEqual(first["relation_id"], second["relation_id"])
        self.assertEqual(len(store.relations(a_id)), 1)
        self.assertEqual(len(store.relations(b_id)), 1)

    def test_contradiction_reduces_both_real_memories_confidence(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation("failure", "provider", "retrieval", "provider result failed", "observed",
                        provider="tavily", error_code="provider_error", signature="same-context-fail"),
            run_id="f3" * 16, artifact_name="result.json",
            artifact_sha256="d" * 64, outcome_status="failed",
        )
        b_id, _ = store.upsert(
            Observation("success", "success", "retrieval", "provider result succeeded", "validated",
                        provider="tavily", signature="same-context-success"),
            run_id="f4" * 16, artifact_name="result.json",
            artifact_sha256="e" * 64, outcome_status="completed",
        )
        before_a = store.show(a_id)["confidence"]
        before_b = store.show(b_id)["confidence"]
        store.relate(a_id, b_id, "contradicts", 0.9, "same provider/context opposite outcome")
        after_a = store.show(a_id)
        after_b = store.show(b_id)
        self.assertEqual(after_a["contradiction_count"], 1)
        self.assertEqual(after_b["contradiction_count"], 1)
        self.assertLess(after_a["confidence"], before_a)
        self.assertLess(after_b["confidence"], before_b)

    def test_synthetic_only_memory_cannot_penalize_real_memory(self):
        store = MemoryStore(self.root)
        real_id, _ = store.upsert(
            Observation("success", "success", "retrieval", "real retrieval success", "validated",
                        provider="tavily", signature="shared-context-real"),
            run_id="13" * 16, artifact_name="result.json",
            artifact_sha256="f" * 64, outcome_status="completed",
        )
        synthetic_id, _ = store.upsert(
            Observation("failure", "provider", "retrieval", "fixture retrieval failure", "observed",
                        provider="fixture", error_code="provider_error",
                        signature="shared-context-synthetic", synthetic=True,
                        origin="synthetic_fixture"),
            run_id="14" * 16, artifact_name="result.json",
            artifact_sha256="0" * 64, outcome_status="failed",
        )
        before = store.show(real_id)["confidence"]
        with self.assertRaisesRegex(ValueError, "synthetic"):
            store.relate(
                real_id, synthetic_id, "contradicts", 0.9,
                "synthetic data must not alter real confidence",
            )
        after = store.show(real_id)
        self.assertEqual(after["contradiction_count"], 0)
        self.assertEqual(after["confidence"], before)

    def test_contradiction_never_auto_supersedes(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation("failure", "network", "retrieval", "old network result", "observed",
                        provider="tavily", error_code="timeout", signature="conflict-old"),
            run_id="15" * 16, artifact_name="result.json",
            artifact_sha256="1" * 64, outcome_status="failed",
        )
        b_id, _ = store.upsert(
            Observation("success", "success", "retrieval", "new network result", "validated",
                        provider="tavily", signature="conflict-new"),
            run_id="16" * 16, artifact_name="result.json",
            artifact_sha256="2" * 64, outcome_status="completed",
        )
        store.relate(a_id, b_id, "contradicts", 0.9, "opposing outcome")
        self.assertNotEqual(store.show(a_id)["status"], "superseded")
        self.assertNotEqual(store.show(b_id)["status"], "superseded")

    def test_relation_provenance_is_visible_from_show(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation("failure", "network", "retrieval", "relation source A", "observed",
                        provider="tavily", error_code="timeout", signature="prov-a"),
            run_id="17" * 16, artifact_name="result.json",
            artifact_sha256="3" * 64, outcome_status="failed",
        )
        b_id, _ = store.upsert(
            Observation("failure", "network", "retrieval", "relation source B", "observed",
                        provider="tavily", error_code="transport_error", signature="prov-b"),
            run_id="18" * 16, artifact_name="result.json",
            artifact_sha256="4" * 64, outcome_status="failed",
        )
        store.relate(
            a_id, b_id, "similar", 0.8, "same transport context",
            evidence={"detector": "batch2-test", "rule": "bounded-token-overlap"},
        )
        shown = store.show(a_id)
        relation = shown["relations"][0]
        self.assertEqual(relation["relation_type"], "similar")
        self.assertEqual(relation["other_memory_id"], b_id)
        self.assertEqual(relation["evidence"]["detector"], "batch2-test")



    def test_schema_v3_migrates_additively_to_v4_and_preserves_history(self):
        import sqlite3

        db_dir = self.root / "memory"
        db_dir.mkdir(parents=True, exist_ok=True)
        db = db_dir / "sira_memory.sqlite3"
        now = "2026-09-20T00:00:00+00:00"
        memory_id = "m_" + ("9" * 32)

        with closing(sqlite3.connect(db)) as conn:
            conn.executescript("""
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE memories (
                    memory_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    provider TEXT,
                    error_code TEXT,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    real_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    synthetic_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    origin_kind TEXT NOT NULL DEFAULT 'run_artifact',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    contradiction_count INTEGER NOT NULL DEFAULT 0,
                    source_diversity INTEGER NOT NULL DEFAULT 1,
                    supersedes_memory_id TEXT,
                    superseded_by_memory_id TEXT,
                    lineage_version INTEGER NOT NULL DEFAULT 1,
                    cluster_key TEXT,
                    cluster_size INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE occurrences (
                    occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    artifact_name TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    outcome_status TEXT,
                    observed_at TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    origin_kind TEXT NOT NULL DEFAULT 'run_artifact',
                    is_synthetic INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(memory_id, run_id, artifact_sha256)
                );
                CREATE TABLE transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    run_id TEXT,
                    changed_at TEXT NOT NULL
                );
                CREATE TABLE memory_relations (
                    relation_id TEXT PRIMARY KEY,
                    memory_a_id TEXT NOT NULL,
                    memory_b_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("INSERT INTO metadata(key,value) VALUES('schema_version','3')")
            conn.execute(
                """INSERT INTO memories(
                    memory_id,fingerprint,kind,category,capability,provider,error_code,
                    summary,status,first_seen_at,last_seen_at,occurrence_count,
                    real_occurrence_count,synthetic_occurrence_count,origin_kind,confidence,
                    contradiction_count,source_diversity,supersedes_memory_id,
                    superseded_by_memory_id,lineage_version,cluster_key,cluster_size
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memory_id, "9" * 64, "failure", "network", "retrieval", "tavily",
                    "timeout", "historical retrieval timeout", "observed",
                    now, now, 1, 1, 0, "run_artifact", 0.3, 0, 1,
                    None, None, 1, "cl_" + ("8" * 32), 1,
                ),
            )
            conn.execute(
                """INSERT INTO occurrences(
                    memory_id,run_id,artifact_name,artifact_sha256,outcome_status,
                    observed_at,details_json,origin_kind,is_synthetic
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    memory_id, "a" * 32, "result.json", "7" * 64, "failed",
                    now, "{}", "run_artifact", 0,
                ),
            )
            conn.commit()

        store = MemoryStore(self.root)
        stats = store.stats()
        self.assertEqual(stats["schema_version"], 4)
        self.assertEqual(stats["total_memories"], 1)
        self.assertEqual(stats["total_occurrences"], 1)
        shown = store.show(memory_id)
        self.assertEqual(shown["summary"], "historical retrieval timeout")
        self.assertEqual(shown["outcomes"], [])

        with closing(sqlite3.connect(db)) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("memory_outcomes", tables)

    def test_record_outcome_round_trips_bounded_provenance(self):
        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "retrieval completed successfully", "validated",
                provider="tavily", signature="batch3-outcome-roundtrip",
            ),
            run_id="31" * 16, artifact_name="result.json",
            artifact_sha256="1" * 64, outcome_status="completed",
        )
        row = store.record_outcome(
            memory_id,
            "retrieval_used",
            context={"consumer": "improvement.research_memory", "query": "retrieval"},
            weight=1.0,
        )
        self.assertEqual(row["memory_id"], memory_id)
        self.assertEqual(row["outcome_type"], "retrieval_used")
        self.assertEqual(row["context"]["consumer"], "improvement.research_memory")
        self.assertEqual(store.outcomes(memory_id)[0]["outcome_id"], row["outcome_id"])
        self.assertEqual(store.show(memory_id)["outcomes"][0]["outcome_id"], row["outcome_id"])

    def test_record_outcome_rejects_unknown_kind_and_invalid_weight(self):
        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval",
                "bounded outcome validation", "observed",
                provider="tavily", error_code="timeout",
                signature="batch3-outcome-validation",
            ),
            run_id="32" * 16, artifact_name="result.json",
            artifact_sha256="2" * 64, outcome_status="failed",
        )
        with self.assertRaisesRegex(ValueError, "outcome"):
            store.record_outcome(memory_id, "owner_preference", context={})
        with self.assertRaisesRegex(ValueError, "weight"):
            store.record_outcome(
                memory_id, "retrieval_used", context={}, weight=1000.0
            )

    def test_retrieve_returns_ranked_score_with_explanations(self):
        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "Tavily retrieval completed successfully", "validated",
                provider="tavily", signature="batch3-retrieve-explain",
            ),
            run_id="33" * 16, artifact_name="result.json",
            artifact_sha256="3" * 64, outcome_status="completed",
        )
        rows = store.retrieve("tavily retrieval", 10)
        result = next(row for row in rows if row["memory_id"] == memory_id)
        self.assertIsInstance(result["retrieval_score"], float)
        self.assertGreaterEqual(result["retrieval_score"], 0.0)
        self.assertLessEqual(result["retrieval_score"], 1.0)
        self.assertIsInstance(result["score_reasons"], list)
        self.assertTrue(result["score_reasons"])
        self.assertTrue(any(x["component"] == "relevance" for x in result["score_reasons"]))
        self.assertTrue(any(x["component"] == "confidence" for x in result["score_reasons"]))
        self.assertTrue(any(x["component"] == "freshness" for x in result["score_reasons"]))

    def test_helped_feedback_boosts_otherwise_equivalent_retrieval(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "provider retrieval strategy alpha", "validated",
                provider="tavily", signature="batch3-help-a",
            ),
            run_id="34" * 16, artifact_name="result.json",
            artifact_sha256="4" * 64, outcome_status="completed",
        )
        b_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "provider retrieval strategy beta", "validated",
                provider="tavily", signature="batch3-help-b",
            ),
            run_id="35" * 16, artifact_name="result.json",
            artifact_sha256="5" * 64, outcome_status="completed",
        )
        store.record_outcome(
            b_id, "retrieval_helped",
            context={"consumer": "batch3-test"}, weight=1.0,
        )
        rows = store.retrieve("provider retrieval strategy", 10)
        scores = {row["memory_id"]: row["retrieval_score"] for row in rows}
        self.assertGreater(scores[b_id], scores[a_id])

    def test_irrelevant_feedback_penalizes_otherwise_equivalent_retrieval(self):
        store = MemoryStore(self.root)
        a_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "retrieval routing lesson alpha", "validated",
                provider="tavily", signature="batch3-irrelevant-a",
            ),
            run_id="36" * 16, artifact_name="result.json",
            artifact_sha256="6" * 64, outcome_status="completed",
        )
        b_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "retrieval routing lesson beta", "validated",
                provider="tavily", signature="batch3-irrelevant-b",
            ),
            run_id="37" * 16, artifact_name="result.json",
            artifact_sha256="7" * 64, outcome_status="completed",
        )
        store.record_outcome(
            b_id, "retrieval_irrelevant",
            context={"consumer": "batch3-test"}, weight=1.0,
        )
        rows = store.retrieve("retrieval routing lesson", 10)
        scores = {row["memory_id"]: row["retrieval_score"] for row in rows}
        self.assertGreater(scores[a_id], scores[b_id])

    def test_old_memory_decays_but_is_not_deleted_or_hidden(self):
        import sqlite3

        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "durable old retrieval lesson", "validated",
                provider="tavily", signature="batch3-old-memory",
            ),
            run_id="38" * 16, artifact_name="result.json",
            artifact_sha256="8" * 64, outcome_status="completed",
        )
        with closing(sqlite3.connect(store.db_path)) as conn:
            conn.execute(
                "UPDATE memories SET last_seen_at=? WHERE memory_id=?",
                ("2020-01-01T00:00:00+00:00", memory_id),
            )
            conn.commit()

        rows = store.retrieve("durable old retrieval lesson", 10)
        self.assertIn(memory_id, {row["memory_id"] for row in rows})
        self.assertEqual(store.show(memory_id)["memory_id"], memory_id)
        result = next(row for row in rows if row["memory_id"] == memory_id)
        self.assertTrue(any(
            x["component"] == "freshness" and x["effect"] == "penalty"
            for x in result["score_reasons"]
        ))

    def test_autonomous_retrieve_excludes_synthetic_only_memory(self):
        store = MemoryStore(self.root)
        real_id, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval",
                "real autonomous retrieval timeout", "observed",
                provider="tavily", error_code="timeout",
                signature="batch3-auto-real",
            ),
            run_id="39" * 16, artifact_name="result.json",
            artifact_sha256="9" * 64, outcome_status="failed",
        )
        synthetic_id, _ = store.upsert(
            Observation(
                "failure", "network", "retrieval",
                "synthetic autonomous retrieval timeout", "observed",
                provider="fixture", error_code="timeout",
                signature="batch3-auto-synthetic",
                origin="synthetic_fixture", synthetic=True,
            ),
            run_id="3a" * 16, artifact_name="result.json",
            artifact_sha256="a" * 64, outcome_status="failed",
        )
        rows = store.retrieve("autonomous retrieval timeout", 10, autonomous=True)
        ids = {row["memory_id"] for row in rows}
        self.assertIn(real_id, ids)
        self.assertNotIn(synthetic_id, ids)

    def test_contradiction_penalty_is_visible_in_retrieval_reasons(self):
        store = MemoryStore(self.root)
        fail_id, _ = store.upsert(
            Observation(
                "failure", "provider", "retrieval",
                "provider request pattern failed", "observed",
                provider="tavily", error_code="provider_error",
                signature="batch3-contradiction-fail",
            ),
            run_id="3b" * 16, artifact_name="result.json",
            artifact_sha256="b" * 64, outcome_status="failed",
        )
        success_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "provider request pattern succeeded", "validated",
                provider="tavily", signature="batch3-contradiction-success",
            ),
            run_id="3c" * 16, artifact_name="result.json",
            artifact_sha256="c" * 64, outcome_status="completed",
        )
        store.relate(
            fail_id, success_id, "contradicts", 0.9,
            "same provider/context opposite outcome",
        )
        rows = store.retrieve("provider request pattern", 10)
        result = next(row for row in rows if row["memory_id"] == success_id)
        self.assertTrue(any(
            x["component"] == "contradiction" and x["effect"] == "penalty"
            for x in result["score_reasons"]
        ))

    def test_search_compatibility_remains_unmodified_by_retrieval_feedback(self):
        store = MemoryStore(self.root)
        memory_id, _ = store.upsert(
            Observation(
                "success", "success", "retrieval",
                "search compatibility retrieval lesson", "validated",
                provider="tavily", signature="batch3-search-compat",
            ),
            run_id="3d" * 16, artifact_name="result.json",
            artifact_sha256="d" * 64, outcome_status="completed",
        )
        before = [row["memory_id"] for row in store.search("search compatibility", 20)]
        store.record_outcome(
            memory_id, "retrieval_irrelevant",
            context={"consumer": "batch3-test"}, weight=1.0,
        )
        after = [row["memory_id"] for row in store.search("search compatibility", 20)]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
