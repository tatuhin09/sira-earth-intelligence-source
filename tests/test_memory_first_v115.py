"""A79: memory reuse requires current, independent, relevant provenance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.memory_first import resolve_memory_first
from sira.learning_goal_adaptive import preview_next_question
from sira.learning_goals import LearningGoalStore
from sira.learning_goal_runtime import run_learning_goal_target
from sira.learning_goal_general_engine import run_general_learning


CLAIM = 'Urban tree canopy can reduce daytime heat in neighborhoods by providing shade.'
QUESTION = 'Urban tree canopy daytime heat'
URLS = ('https://www.nasa.gov/urban-tree-canopy', 'https://www.noaa.gov/urban-tree-canopy')


class MemoryFirstTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = KnowledgeConsolidationStore(self.root)
        self.now = datetime.now(timezone.utc)

    def add(self, claim=CLAIM, *, key='claim.urban.heat', hosts=2, confidence=.9, days_ago=0):
        for index, url in enumerate(URLS[:hosts]):
            self.store.record_evidence(key, claim, evidence_id=f'ev.{index}.{len(claim)}',
                source_id=f'S{index+1}', source_url=url, confidence=confidence,
                verifier_kind='readable_official_source', evidence_sha256='a'*64,
                retrieved_at=(self.now-timedelta(days=days_ago)).isoformat(), verified=True)
        return self.store.consolidate(key, stale_after_days=180)

    def test_verified_fresh_relevant_resolves_without_network_or_model(self):
        self.add()
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            result = resolve_memory_first(self.root, QUESTION)
        self.assertEqual(result['status'], 'memory_resolved')
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(result['metered_model_requests'], 0)
        self.assertEqual(result['results'][0]['claim'], CLAIM)

    def test_missing_memory_requests_research(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(empty))
        result = resolve_memory_first(empty, QUESTION)
        self.assertEqual(result['status'], 'research_needed')
        self.assertEqual(result['reason'], 'no_verified_memory')
        self.assertEqual(result['api_requests'], 0)

    def test_single_or_weak_source_cannot_resolve(self):
        self.assertEqual(self.add(hosts=1).status, 'pending')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['status'], 'research_needed')
        self.assertFalse(self.store.record_evidence('claim.unverified', CLAIM,
            evidence_id='ev.unverified', source_id='S3', source_url='https://example.org/claim',
            confidence=.95, verifier_kind='unverified', evidence_sha256='b'*64,
            retrieved_at=self.now.isoformat(), verified=False))
        self.assertEqual(self.store.lookup('claim.unverified'), None)

    def test_stale_memory_requests_revalidation(self):
        self.add(days_ago=200)
        result = resolve_memory_first(self.root, QUESTION)
        self.assertEqual(result['reason'], 'stale_memory_requires_revalidation')
        self.assertTrue(result['research_required'])

    def test_one_recent_host_does_not_hide_stale_independent_source(self):
        self.store.record_evidence('claim.urban.heat', CLAIM,
            evidence_id='ev.old', source_id='S1', source_url=URLS[0],
            confidence=.9, verifier_kind='readable_official_source', evidence_sha256='a'*64,
            retrieved_at=(self.now-timedelta(days=200)).isoformat(), verified=True)
        self.store.record_evidence('claim.urban.heat', CLAIM,
            evidence_id='ev.new', source_id='S2', source_url=URLS[1],
            confidence=.9, verifier_kind='readable_official_source', evidence_sha256='b'*64,
            retrieved_at=self.now.isoformat(), verified=True)
        self.assertEqual(self.store.consolidate('claim.urban.heat').status, 'consolidated')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['reason'],
                         'stale_independent_provenance')

    def test_contradicted_key_fails_closed(self):
        self.add()
        self.store.record_evidence('claim.urban.heat',
            'Urban tree canopy can not reduce daytime heat in neighborhoods by providing shade.',
            evidence_id='ev.contra', source_id='S3', source_url='https://www.epa.gov/urban-tree',
            confidence=.9, verifier_kind='readable_official_source', evidence_sha256='b'*64,
            retrieved_at=self.now.isoformat(), verified=True)
        self.assertEqual(self.store.consolidate('claim.urban.heat').status, 'blocked')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['reason'], 'contradicted_memory')

    def test_separate_opposing_verified_keys_fail_closed(self):
        self.add()
        opposing = 'Urban tree canopy can not reduce daytime heat in neighborhoods by providing shade.'
        self.add(opposing, key='claim.urban.heat.opposing')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['reason'], 'contradictory_verified_claims')

    def test_irrelevant_verified_memory_is_not_used(self):
        self.add()
        result = resolve_memory_first(self.root, 'OAuth token revocation and browser sessions')
        self.assertEqual(result['status'], 'research_needed')
        self.assertEqual(result['reason'], 'no_relevant_verified_memory')

    def test_exact_provenance_and_bounded_context(self):
        self.add()
        result = resolve_memory_first(self.root, QUESTION)
        sources = result['results'][0]['provenance']
        self.assertEqual({row['source_url'] for row in sources}, set(URLS))
        self.assertEqual({row['evidence_sha256'] for row in sources}, {'a'*64})
        self.assertEqual({row['verifier_kind'] for row in sources}, {'readable_official_source'})
        self.assertEqual(len(result['results']), 1)
        self.assertNotIn('raw_source_text', str(result))

    def test_explicit_fresh_request_bypasses_resolution_with_context(self):
        self.add()
        for request in ('latest Urban tree canopy daytime heat', QUESTION):
            result = resolve_memory_first(self.root, request,
                request_fresh_research=request == QUESTION)
            self.assertEqual(result['reason'], 'explicit_fresh_research_requested')
            self.assertEqual(result['local_knowledge_keys'], ['claim.urban.heat'])
            self.assertEqual(result['api_requests'], 0)

    def test_a77_general_claim_is_reusable(self):
        goal_store = LearningGoalStore(self.root)
        goal = goal_store.create('Urban climate planning', public_research_allowed=True)
        goal_store.set_plan(goal['goal_id'], ['Urban tree canopy and daytime heat', 'Stormwater and drainage'])
        research = {'knowledge': {'results': [{'title': 'Urban tree canopy and daytime heat', 'url': URLS[0]}]},
                    'open_access': {'results': [{'title': 'Urban tree canopy and daytime heat', 'url': URLS[1]}]}}
        result = run_general_learning(self.root, goal['goal_id'], research,
            question='Urban tree canopy and daytime heat',
            fetcher=lambda url: (url, 'text/html', ('<html><main><p>'+CLAIM+'</p></main></html>').encode()))
        self.assertEqual(result['status'], 'verified_knowledge_recorded')
        self.assertEqual(resolve_memory_first(self.root, QUESTION)['status'], 'memory_resolved')
        self.assertFalse((self.root / 'memory/learning_skill_lifecycle').exists())

    def test_a78_skips_material_verified_by_other_goal(self):
        self.add()
        goals = LearningGoalStore(self.root)
        goal = goals.create('Urban climate adaptation', public_research_allowed=True)
        goal = goals.set_plan(goal['goal_id'], [QUESTION, 'Stormwater drainage'])
        decision = preview_next_question(self.root, goal)
        self.assertEqual(decision['focus'], 'Stormwater drainage')
        self.assertEqual(decision['reason'], 'unresolved_new_gap')
        with patch('socket.create_connection', side_effect=AssertionError('network')):
            self.assertEqual(resolve_memory_first(self.root, QUESTION)['status'], 'memory_resolved')
        self.assertFalse((self.root / 'memory/learning_skill_lifecycle').exists())

    def test_unplanned_goal_memory_hit_preempts_external_search(self):
        self.add()
        goals = LearningGoalStore(self.root)
        goal = goals.create(QUESTION, public_research_allowed=True)
        target = {'target_kind': 'learning_goal', 'learning_goal_id': goal['goal_id'], 'topic': goal['topic']}
        def forbidden(*args, **kwargs):
            raise AssertionError('External source was called after verified memory hit')
        report = run_learning_goal_target(self.root, target,
            knowledge_searcher=forbidden, open_access_searcher=forbidden)
        self.assertEqual(report['outcome'], 'local_verified_knowledge_reused')
        self.assertEqual(report['memory_resolution']['status'], 'memory_resolved')
        self.assertEqual(report['api_requests'], 0)
        self.assertEqual(report['metered_model_requests'], 0)
        self.assertFalse(report['promotion_performed'])

    def test_no_skill_activation_and_bounded_retrieval(self):
        for index in range(8):
            self.add(CLAIM + f' Verified variation {index}.', key=f'claim.urban.variation.{index}')
        result = resolve_memory_first(self.root, QUESTION, limit=2)
        self.assertEqual(result['status'], 'memory_resolved')
        self.assertLessEqual(len(result['results']), 2)
        self.assertFalse(result['skill_activated'])
        self.assertFalse((self.root / 'memory/learning_skill_lifecycle').exists())


if __name__ == '__main__':
    unittest.main()
