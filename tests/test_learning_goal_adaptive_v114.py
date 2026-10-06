import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sira.learning_goal_adaptive import (preview_next_question, record_progression,
                                         classify_outcome, EVIDENCE_BACKOFF)
from sira.learning_goals import LearningGoalStore
from sira.autonomous_targeting import _learning_goal_candidates
from sira.learning_goal_runtime import run_learning_goal_target


class AdaptiveProgressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = LearningGoalStore(self.root)
        self.goal = self.store.create('Urban planning', priority='high', public_research_allowed=True)
        self.goal = self.store.set_plan(self.goal['goal_id'], ['Urban tree canopy', 'Walkable streets', 'Transit access'])
        self.now = 1000000.0

    def decide(self, evidence=None, goal=None, at=None):
        return preview_next_question(self.root, goal or self.goal, now_epoch=self.now if at is None else at,
                                     evidence={} if evidence is None else evidence)

    def test_verified_evidence_moves_to_unresolved_gap(self):
        decision = self.decide({'Urban tree canopy': ['verified narrow claim']})
        self.assertEqual(decision['focus'], 'Walkable streets')
        self.assertEqual(decision['reason'], 'unresolved_new_gap')
        self.assertNotIn('mastery', decision)

    def test_recent_failed_duplicate_suppressed(self):
        first = self.decide()
        record_progression(self.root, self.goal['goal_id'], first, outcome='research_sources_insufficient', at_epoch=self.now)
        second = self.decide()
        self.assertEqual(second['focus'], 'Walkable streets')
        self.assertEqual(second['suppressed'][0]['reason'], 'evidence_gap_backoff')
        self.assertEqual(self.decide(at=self.now + EVIDENCE_BACKOFF + 1)['focus'], 'Urban tree canopy')

    def test_provider_failure_classification_and_shorter_backoff(self):
        first = self.decide()
        event = record_progression(self.root, self.goal['goal_id'], first, outcome='research_failed', at_epoch=self.now)
        self.assertEqual(event['classification'], 'provider_failure')
        self.assertEqual(self.decide()['focus'], 'Walkable streets')
        self.assertEqual(self.decide(at=self.now + 1801)['focus'], 'Urban tree canopy')
        self.assertEqual(classify_outcome('research_sources_insufficient'), 'evidence_gap')

    def test_cooling_provider_does_not_suppress_other_step(self):
        first = self.decide()
        record_progression(self.root, self.goal['goal_id'], first, outcome='deferred_provider_cooldown', at_epoch=self.now)
        self.assertEqual(self.decide()['focus'], 'Walkable streets')

    def test_another_owner_goal_remains_available(self):
        other = self.store.create('River ecology', public_research_allowed=True)
        other = self.store.set_plan(other['goal_id'], ['River restoration', 'Wetland health'])
        for index in range(3):
            decision = self.decide()
            record_progression(self.root, self.goal['goal_id'], decision, outcome='research_sources_insufficient', at_epoch=self.now)
        self.assertIsNone(self.decide())
        with patch('sira.learning_goal_adaptive.verified_study_step_evidence', return_value={}):
            selected = _learning_goal_candidates(self.root, 10, self.now)
        self.assertEqual([row['learning_goal_id'] for row in selected], [other['goal_id']])

    def test_contradiction_followup(self):
        self.goal['study_step_stats'][0].update(attempt_count=1, last_outcome='contradictory_evidence', last_attempt_at_epoch=self.now - 100)
        self.assertEqual(self.decide()['reason'], 'resolve_contradiction')
        self.assertIn('conflicting source evidence', self.decide()['question'])

    def test_weak_evidence_followup(self):
        self.goal['study_step_stats'][0].update(attempt_count=1, last_outcome='sources_discovered_needs_verification', last_attempt_at_epoch=self.now - 100)
        # A saved report distinguishes weak readable text from ordinary discovery.
        reports = self.root / 'memory' / 'learning_goal_reports'
        reports.mkdir()
        path = reports / 'weak.json'
        path.write_text(json.dumps({'learning_goal_id': self.goal['goal_id'], 'research_query': 'Urban tree canopy',
                                    'document_review': {'status': 'unverified_source_statements'}}))
        self.goal['last_report'] = str(path)
        self.assertEqual(self.decide()['reason'], 'strengthen_weak_evidence')

    def test_repeated_coverage_is_not_mastery(self):
        result = self.decide({'Urban tree canopy': ['claim 1', 'claim 2']})
        self.assertEqual(result['focus'], 'Walkable streets')
        self.assertNotIn('mastery', result)

    def test_decision_persists_provenance_and_outcome(self):
        decision = self.decide()
        event = record_progression(self.root, self.goal['goal_id'], decision, outcome='ambiguous_evidence', at_epoch=self.now)
        saved = json.loads((self.root / 'memory' / 'learning_goal_adaptive' / (self.goal['goal_id'] + '.json')).read_text())
        self.assertEqual(saved['events'][0], event)
        self.assertEqual(event['fingerprint'], decision['fingerprint'])
        self.assertEqual(event['classification'], 'evidence_gap')
        with self.assertRaises(ValueError):
            record_progression(self.root, self.goal['goal_id'], {**decision, 'fingerprint': '0'*64}, outcome='verified_knowledge_recorded')

    def test_owner_priority_over_background(self):
        with patch('sira.learning_goal_adaptive.verified_study_step_evidence', return_value={}):
            result = _learning_goal_candidates(self.root, 10, self.now)
        self.assertEqual(result[0]['priority_class_name'], 'owner_learning_goal')
        self.assertEqual(result[0]['effective_priority_score'], 10000)

    def test_general_engine_and_skills_untouched(self):
        from sira.learning_goal_general_engine import assess_general_documents
        from sira.learning_goal_practice_verification import verify_practice_run
        self.assertTrue(callable(assess_general_documents))
        self.assertTrue(callable(verify_practice_run))
        self.assertFalse((self.root / 'memory' / 'skills').exists())

    def test_atomic_step_choice_stale_rejected(self):
        self.store.choose_study_step(self.goal['goal_id'], expected_index=0, selected_index=1)
        self.assertEqual(self.store.get(self.goal['goal_id'])['study_next_index'], 1)
        with self.assertRaises(ValueError):
            self.store.choose_study_step(self.goal['goal_id'], expected_index=0, selected_index=2)

    def test_bounded_cycle_records_selection_and_result_without_skill(self):
        def search(_root, _query, *, max_results):
            return {'results': [], 'metrics': {'api_requests': 0}}
        target = {'target_kind': 'learning_goal', 'learning_goal_id': self.goal['goal_id'],
                  'topic': self.goal['topic']}
        report = run_learning_goal_target(self.root, target, now_epoch=self.now,
                                          knowledge_searcher=search, open_access_searcher=search)
        saved = json.loads((self.root / 'memory' / 'learning_goal_adaptive' /
                            (self.goal['goal_id'] + '.json')).read_text())['events'][-1]
        self.assertEqual(report['adaptive_selection']['focus'], 'Urban tree canopy')
        self.assertEqual(saved['outcome'], 'research_sources_insufficient')
        self.assertEqual(saved['report_artifact'], report['artifact'])
        self.assertEqual(report['metered_model_requests'], 0)
        self.assertFalse(report['promotion_performed'])
        self.assertFalse((self.root / 'memory' / 'learning_skill_lifecycle').exists())
