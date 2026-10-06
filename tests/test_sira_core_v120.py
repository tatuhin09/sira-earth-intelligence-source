"""A83: bounded shared SIRA state and dispatch contracts."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from sira.learning_goals import LearningGoalStore
from sira.knowledge_consolidation import KnowledgeConsolidationStore
from sira.runtime import RuntimeStateStore
from sira.sira_core import SiraCore, CoreStateError

CLAIM='Urban tree canopy can reduce daytime heat in neighborhoods by providing shade.'

class CoreTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='sira-a83-')
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.core=SiraCore(self.root)
        self.git=patch('sira.self_model._git_head',return_value='abc1234')
        self.git.start(); self.addCleanup(self.git.stop)
        self.health=patch('sira.self_model.build_provider_health_snapshot',return_value={
            'provider_count':1,'available_count':1,'active_cooldown_count':0,
            'providers':[{'provider_id':'wikimedia','health':'ready','available_now':True,
                         'cost_class':'free','cooldown_active':False,'historical_failures':0}]})
        self.health.start(); self.addCleanup(self.health.stop)

    def memory(self):
        store=KnowledgeConsolidationStore(self.root)
        now=datetime.now(timezone.utc).isoformat()
        for i,host in enumerate(('www.nasa.gov','www.noaa.gov')):
            store.record_evidence('claim.tree',CLAIM,evidence_id=f'e{i}',source_id=f's{i}',
                source_url=f'https://{host}/trees',confidence=.91,verifier_kind='independent_read',
                evidence_sha256=chr(97+i)*64,retrieved_at=now,verified=True)
        self.assertEqual(store.consolidate('claim.tree').status,'consolidated')

    def test_identity_shared_between_snapshot_and_all_routes(self):
        results=[self.core.dispatch('inspect_self'),self.core.dispatch('general',text='hello'),
                 self.core.dispatch('answer_from_memory',text='unknown subject')]
        for result in results: self.assertEqual(result['identity']['name'],'SIRA')
        self.assertEqual(self.core.snapshot()['identity']['head'],'abc1234')

    def test_verified_memory_hit_avoids_research_and_network(self):
        self.memory()
        with patch('sira.sira_core.create_research_job',side_effect=AssertionError('research invoked')):
            result=self.core.dispatch('answer_from_memory',text=CLAIM,allow_research=True)
        self.assertEqual(result['route'],'verified_memory')
        self.assertEqual(result['result']['status'],'memory_resolved')
        self.assertEqual(result['result']['results'][0]['knowledge_key'],'claim.tree')
        self.assertEqual(result['resource_usage']['api_requests'],0)

    def test_missing_memory_routes_only_permitted_research(self):
        with patch('sira.sira_core.create_research_job',side_effect=AssertionError('unpermitted')):
            no=self.core.dispatch('answer_from_memory',text='Ocean salinity changes')
        self.assertEqual(no['route'],'research_needed')
        self.assertEqual(no['task']['status'],'blocked')
        yes=self.core.dispatch('answer_from_memory',text='Ocean salinity changes',allow_research=True)
        self.assertEqual(yes['route'],'research_job')
        self.assertEqual(yes['task']['status'],'pending')
        self.assertTrue(yes['task']['artifact_refs'][0].startswith('dr_'))
        self.assertEqual(yes['resource_usage']['api_requests'],0)

    def test_explicit_fresh_research_bypasses_old_memory_answer(self):
        self.memory()
        result=self.core.dispatch('research',text='Latest '+CLAIM,allow_research=True)
        self.assertEqual(result['route'],'research_job')
        self.assertEqual(result['memory_decision']['reason'],'explicit_fresh_research_requested')
        self.assertEqual(result['task']['status'],'pending')

    def test_research_queues_evidence_only_and_never_writes_verified_knowledge(self):
        result=self.core.dispatch('research',text='Deep research urban trees',allow_research=True)
        self.assertEqual(result['task']['status'],'pending')
        self.assertEqual(result['task']['verification_status'],'not_verified')
        self.assertFalse((self.root/'memory/sira_knowledge.sqlite3').exists())

    def test_learning_is_goal_bound_and_does_not_activate_skill(self):
        goal=LearningGoalStore(self.root).create('Python software testing')
        result=self.core.dispatch('learn',goal_id=goal['goal_id'])
        self.assertEqual(result['route'],'existing_learning_pipeline')
        self.assertEqual(result['task']['status'],'pending')
        self.assertFalse((self.root/'memory/learning_skill_lifecycle').exists())
        with self.assertRaises(ValueError): self.core.dispatch('learn',goal_id='lg_'+'f'*32)

    def test_direct_user_task_outweighs_background_learning(self):
        goal=LearningGoalStore(self.root).create('Python software testing')
        bg=self.core.dispatch('learn',goal_id=goal['goal_id'],origin='background')
        direct=self.core.dispatch('answer_from_memory',text='an unanswered direct question',allow_research=True)
        snapshot=self.core.snapshot()
        self.assertEqual(snapshot['current_direct_user_task'],direct['task']['task_id'])
        self.assertEqual(snapshot['pending_work'][0]['task_id'],direct['task']['task_id'])
        self.assertEqual(snapshot['pending_work'][1]['task_id'],bg['task']['task_id'])
        self.assertEqual(snapshot['preemption']['policy'],'finish_or_cancel_bounded_current_operation')

    def test_background_cannot_preempt_user_priority(self):
        direct=self.core.dispatch('research',text='Owner research',allow_research=True)
        goal=LearningGoalStore(self.root).create('Software testing')
        self.core.dispatch('learn',goal_id=goal['goal_id'],origin='background')
        self.assertEqual(self.core.snapshot()['pending_work'][0]['task_id'],direct['task']['task_id'])

    def test_self_model_cannot_grant_authority(self):
        actual=self.core.snapshot()['self_model']
        with patch('sira.sira_core.collect_self_model') as model:
            model.return_value={**actual,'authority_granted':True,
                                'promotion_authorized':True,'paid_spending_authorized':True}
            result=self.core.dispatch('engineering',text='Improve code')
        self.assertEqual(result['task']['status'],'blocked')
        self.assertEqual(result['route'],'self_model_integrity_blocked')

    def test_engineering_remains_pending_existing_protected_pipeline(self):
        with patch('sira.engineering_runtime.run_engineering_runtime_handoff',side_effect=AssertionError('bypass')):
            result=self.core.dispatch('engineering',text='Improve local tests')
        self.assertEqual(result['route'],'protected_engineering_pipeline')
        self.assertEqual(result['task']['status'],'pending')
        self.assertFalse(result['task']['promotion_performed'])
        self.assertFalse(result['task']['authority_granted'])

    def test_stop_state_is_not_modified_by_inspection(self):
        RuntimeStateStore(self.root).save({'desired_state':'off','worker_state':'stopped','generation':66})
        result=self.core.dispatch('inspect_self')
        self.assertEqual(result['state']['runtime']['effective_state'],'stopped')
        self.assertEqual(RuntimeStateStore(self.root).status()['desired_state'],'off')

    def test_persistent_tasks_across_runtime_generation_update(self):
        first=self.core.dispatch('research',text='Study provenance',allow_research=True)
        RuntimeStateStore(self.root).save({'desired_state':'off','worker_state':'stopped','generation':67})
        other=SiraCore(self.root).snapshot()
        self.assertEqual(other['runtime']['generation'],67)
        self.assertEqual(other['pending_work'][0]['task_id'],first['task']['task_id'])

    def test_corrupt_core_state_fails_closed_without_damaging_goal(self):
        goal=LearningGoalStore(self.root).create('Python testing')
        self.core.dispatch('inspect_self')
        path=self.root/'runtime/core/tasks.json'
        path.write_text('{broken')
        with self.assertRaises(CoreStateError): self.core.dispatch('learn',goal_id=goal['goal_id'])
        self.assertEqual(LearningGoalStore(self.root).get(goal['goal_id'])['status'],'active')

    def test_missing_current_after_interrupted_write_recovers_previous_snapshot(self):
        first=self.core.dispatch('research',text='Study provenance',allow_research=True)
        self.core.dispatch('inspect_self')
        path=self.root/'runtime/core/tasks.json'
        path.unlink()  # simulate crash between atomic snapshot rotation and write
        recovered=SiraCore(self.root).snapshot()
        self.assertEqual(recovered['pending_work'][0]['task_id'],first['task']['task_id'])

    def test_forged_core_authority_fields_fail_closed(self):
        self.core.dispatch('inspect_self')
        path=self.root/'runtime/core/tasks.json'
        data=json.loads(path.read_text())
        data['authority_granted']=True
        from sira.sira_core import _digest
        data['sha256']=_digest({k:v for k,v in data.items() if k!='sha256'})
        path.write_text(json.dumps(data))
        with self.assertRaises(CoreStateError): self.core.snapshot()

    def test_worker_state_not_fabricated(self):
        state=self.core.snapshot()
        self.assertEqual(state['workers']['recent_tasks'],[])
        self.assertIsNone(state['workers']['active_cycle'])

    def test_invalid_intent_and_unapproved_paid_spending_fail_closed(self):
        with self.assertRaises(ValueError): self.core.dispatch('shell',text='rm -rf /')
        with self.assertRaises(ValueError): self.core.dispatch('research',text='x',allow_paid=True)
        result=self.core.dispatch('research',text='research proof',allow_research=True)
        self.assertEqual(result['resource_usage']['paid_requests'],0)

    def test_task_records_are_bounded_and_do_not_store_secret_text(self):
        for i in range(70): self.core.dispatch('general',text=f'private-token-{i}')
        state=self.core.snapshot()
        self.assertLessEqual(len(state['task_history']),64)
        data=(self.root/'runtime/core/tasks.json').read_text()
        self.assertNotIn('private-token',data)
        self.assertFalse(state['authority_granted'])
        self.assertFalse(state['skill_activated'])

if __name__=='__main__': unittest.main()
