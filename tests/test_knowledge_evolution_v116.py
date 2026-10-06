"""A80: lifecycle, freshness, provenance and A79/A78 fail-closed integration."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sira.knowledge_consolidation import KnowledgeConsolidationStore, FRESHNESS_POLICIES
from sira.knowledge_evolution import KnowledgeEvolutionStore
from sira.memory_first import resolve_memory_first
from sira.learning_goal_adaptive import preview_next_question
from sira.learning_goals import LearningGoalStore

BASE = 'Urban tree canopy can reduce daytime heat in neighborhoods by providing shade.'
OPPOSING = 'Urban tree canopy can not reduce daytime heat in neighborhoods by providing shade.'
NEWER = 'Urban tree canopy can reduce daytime heat in neighborhoods through shade and evaporation.'
QUESTION = 'Urban tree canopy daytime heat'
URLS = ('https://www.nasa.gov/tree', 'https://www.noaa.gov/tree',
        'https://www.epa.gov/tree', 'https://www.usgs.gov/tree')


class KnowledgeEvolutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='sira-a80-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = KnowledgeConsolidationStore(self.root)
        self.evo = KnowledgeEvolutionStore(self.root)
        self.now = datetime.now(timezone.utc)

    def add(self, key='claim.tree', claim=BASE, *, age=0, confidence=.9,
            hosts=2, suffix=''):
        for index, url in enumerate(URLS[:hosts]):
            assert self.store.record_evidence(key, claim,
                evidence_id=f'ev.{index}.{key}.{suffix}', source_id=f'S{index}',
                source_url=url, confidence=confidence, verifier_kind='independent_read',
                evidence_sha256=(chr(97 + index) * 64),
                retrieved_at=(self.now - timedelta(days=age)).isoformat(), verified=True)
        self.assertEqual(self.store.consolidate(key).status, 'consolidated')
        return self.store.lookup(key)

    def test_foundational_is_fresh_longer_and_policy_is_explicit(self):
        self.add(age=500)
        self.evo.set_policy('claim.tree', 'foundational', reason='Stable physical mechanism')
        row = self.store.lookup('claim.tree', now=self.now.isoformat())
        self.assertEqual(row['freshness'], 'fresh')
        self.assertEqual(row['effective_stale_after_days'], 730)
        self.assertEqual(row['freshness_reason'], 'Stable physical mechanism')
        self.assertTrue(row['reconsider_after'] > self.now.isoformat())
        self.assertEqual(resolve_memory_first(self.root, QUESTION, now=self.now.isoformat())['status'], 'memory_resolved')

    def test_dynamic_policy_stales_while_engineering_is_fresh(self):
        self.add(age=8)
        self.evo.set_policy('claim.tree', 'dynamic_current', reason='Current measurement changes')
        self.assertEqual(self.store.lookup('claim.tree', now=self.now.isoformat())['freshness'], 'stale')
        self.assertEqual(resolve_memory_first(self.root, QUESTION, now=self.now.isoformat())['reason'],
                         'stale_memory_requires_revalidation')
        self.evo.set_policy('claim.tree', 'stable_engineering', reason='Engineering baseline')
        self.assertEqual(self.store.lookup('claim.tree', now=self.now.isoformat())['freshness'], 'fresh')
        self.assertEqual(FRESHNESS_POLICIES['experience_history'], 365)

    def test_new_stronger_evidence_supersedes_without_deleting_history(self):
        self.add(age=20, confidence=.84)
        self.add('claim.tree.new', NEWER, age=1, confidence=.94, hosts=3)
        event = self.evo.supersede('claim.tree', 'claim.tree.new', reason='Newer broader evidence')
        old = self.store.lookup('claim.tree')
        self.assertEqual(old['freshness'], 'superseded')
        self.assertEqual(old['successor_key'], 'claim.tree.new')
        self.assertEqual(event['successor_key'], 'claim.tree.new')
        self.assertEqual({r['knowledge_key'] for r in event['evidence_refs']},
                         {'claim.tree', 'claim.tree.new'})
        self.assertEqual(self.evo.history('claim.tree')[0]['event_id'], event['event_id'])
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM knowledge_evidence WHERE knowledge_key='claim.tree'").fetchone()[0], 2)
        result = resolve_memory_first(self.root, QUESTION)
        self.assertEqual(result['status'], 'memory_resolved')
        self.assertEqual(result['results'][0]['knowledge_key'], 'claim.tree.new')
        self.assertNotIn('claim.tree', [r['knowledge_key'] for r in result['results']])

    def test_unrelated_or_weaker_replacement_fails_closed(self):
        self.add(age=2)
        self.add('claim.tree.weak', NEWER, age=1, confidence=.81)
        with self.assertRaisesRegex(ValueError, 'stronger'):
            self.evo.supersede('claim.tree', 'claim.tree.weak', reason='Unjustified replacement')
        self.assertEqual(self.store.lookup('claim.tree')['freshness'], 'fresh')

    def test_opposing_independent_claims_are_conflicted_with_provenance(self):
        self.add()
        self.add('claim.tree.no', OPPOSING)
        events = self.evo.mark_conflict('claim.tree', 'claim.tree.no', reason='Opposing source findings')
        self.assertEqual(len(events), 2)
        self.assertEqual({r['knowledge_key'] for r in events[0]['evidence_refs']},
                         {'claim.tree', 'claim.tree.no'})
        self.assertEqual(self.store.lookup('claim.tree')['freshness'], 'conflicted')
        self.assertEqual(self.store.lookup('claim.tree.no')['freshness'], 'conflicted')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['reason'], 'contradicted_memory')
        self.assertEqual(len(self.evo.history('claim.tree')), 1)

    def test_unrelated_claims_cannot_be_marked_as_conflicting(self):
        self.add()
        self.add('claim.tree.new', NEWER)
        with self.assertRaisesRegex(ValueError, 'Opposing'):
            self.evo.mark_conflict('claim.tree', 'claim.tree.new', reason='Unrelated assertion')

    def test_reinforcement_is_atomic_and_duplicate_cannot_inflate(self):
        self.add(confidence=.85)
        result = self.evo.reinforce('claim.tree', evidence_id='ev.third', source_id='S3',
            source_url=URLS[2], confidence=.95, verifier_kind='independent_read',
            evidence_sha256='f'*64, retrieved_at=self.now.isoformat(), verified=True,
            reason='Independent compatible corroboration')
        self.assertEqual(result['status'], 'reinforced')
        row = self.store.lookup('claim.tree')
        self.assertEqual((row['host_count'], row['source_count'], row['evidence_count']), (3, 3, 3))
        self.assertEqual(row['confidence'], round((.85 + .85 + .95)/3, 4))
        self.assertEqual(self.evo.history('claim.tree')[0]['kind'], 'reinforced')
        duplicate = self.evo.reinforce('claim.tree', evidence_id='ev.third', source_id='S3',
            source_url=URLS[2], confidence=.95, verifier_kind='independent_read',
            evidence_sha256='f'*64, retrieved_at=self.now.isoformat(), verified=True,
            reason='Independent compatible corroboration')
        self.assertEqual(duplicate['status'], 'duplicate_evidence')
        self.assertEqual(self.store.lookup('claim.tree')['confidence'], row['confidence'])
        self.assertEqual(len(self.evo.history('claim.tree')), 1)

    def test_repeated_same_host_does_not_inflate_diversity_or_mean(self):
        self.add(confidence=.85)
        self.evo.reinforce('claim.tree', evidence_id='ev.samehost', source_id='S4',
            source_url=URLS[0], confidence=.86, verifier_kind='independent_read',
            evidence_sha256='e'*64, retrieved_at=self.now.isoformat(), verified=True,
            reason='Compatible same host followup')
        row = self.store.lookup('claim.tree')
        self.assertEqual((row['host_count'], row['source_count']), (2, 2))
        self.assertEqual(row['confidence'], .855)

    def test_reinforcement_retains_explicit_policy_and_transition_binding(self):
        self.add()
        self.evo.set_policy('claim.tree', 'foundational', reason='Established mechanism')
        self.evo.reinforce('claim.tree', evidence_id='ev.new.host', source_id='S3',
            source_url=URLS[2], confidence=.93, verifier_kind='independent_read',
            evidence_sha256='d'*64, retrieved_at=self.now.isoformat(), verified=True,
            reason='New independent supporting host')
        row = self.store.lookup('claim.tree')
        self.assertEqual((row['freshness'], row['freshness_class']), ('fresh', 'foundational'))
        self.assertEqual([r['kind'] for r in self.evo.history('claim.tree')],
                         ['reinforced', 'policy_assigned'])

    def test_malformed_lifecycle_and_event_metadata_fail_closed(self):
        self.add()
        self.evo.set_policy('claim.tree', 'dynamic_current', reason='Variable evidence')
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute("UPDATE knowledge_lifecycle SET freshness_class='broken' WHERE knowledge_key='claim.tree'")
        self.assertEqual(self.store.lookup('claim.tree')['freshness'], 'invalid_lifecycle_metadata')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['reason'], 'invalid_lifecycle_metadata')
        with self.assertRaises(ValueError):
            self.evo.deprecate('claim.tree', reason='Malformed metadata')
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute("UPDATE knowledge_lifecycle SET freshness_class='dynamic_current' WHERE knowledge_key='claim.tree'")
            db.execute("UPDATE knowledge_lifecycle_events SET evidence_refs_json='{}'")
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['reason'], 'invalid_lifecycle_metadata')
        with self.assertRaises(ValueError):
            self.evo.history('claim.tree')

    def test_schema_v1_rows_survive_additive_migration(self):
        self.add()
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            evidence_count = db.execute('SELECT COUNT(*) FROM knowledge_evidence').fetchone()[0]
            db.execute('DROP TABLE knowledge_lifecycle_events')
            db.execute('DROP TABLE knowledge_lifecycle')
        migrated = KnowledgeConsolidationStore(self.root)
        self.assertEqual(migrated.stats()['schema_version'], 1)
        self.assertEqual(migrated.lookup('claim.tree')['freshness_class'], 'legacy_policy')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['status'], 'memory_resolved')
        with closing(sqlite3.connect(migrated.db_path)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM knowledge_evidence').fetchone()[0], evidence_count)
            self.assertIn('knowledge_lifecycle', {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})

    def test_adaptive_revalidates_stale_and_resolves_conflict(self):
        self.add(age=8)
        self.evo.set_policy('claim.tree', 'dynamic_current', reason='Dynamic claim')
        goals = LearningGoalStore(self.root)
        goal = goals.create('Urban adaptation', public_research_allowed=True)
        goal = goals.set_plan(goal['goal_id'], [QUESTION, 'Stormwater drainage'])
        decision = preview_next_question(self.root, goal)
        self.assertEqual((decision['focus'], decision['reason']),
                         (QUESTION, 'revalidate_stale_knowledge'))
        self.add('claim.tree.no', OPPOSING)
        self.evo.mark_conflict('claim.tree', 'claim.tree.no', reason='Opposing source findings')
        decision = preview_next_question(self.root, goal)
        self.assertEqual((decision['focus'], decision['reason']),
                         (QUESTION, 'resolve_contradiction'))

    def test_deprecated_is_not_reused_or_revalidated(self):
        self.add()
        self.evo.deprecate('claim.tree', reason='Retired supported assertion')
        self.assertEqual(self.store.lookup('claim.tree')['freshness'], 'deprecated')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['status'], 'research_needed')
        self.assertFalse(any(r['knowledge_key'] == 'claim.tree' for r in self.store.revalidation_candidates()))

    def test_no_network_model_or_skill_activation(self):
        self.add()
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            self.assertEqual(resolve_memory_first(self.root, QUESTION)['status'], 'memory_resolved')
            self.evo.set_policy('claim.tree', 'stable_engineering', reason='Well established')
        self.assertFalse((self.root / 'memory/learning_skill_lifecycle').exists())
        self.assertFalse((self.root / 'memory/skills').exists())
