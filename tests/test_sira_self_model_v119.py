"""A82 evidence-backed persistent descriptive self-model."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sira.self_model import SelfModelStore, capability_gaps, collect_self_model
from sira.runtime import RuntimeStateStore
from sira.learning_goals import LearningGoalStore
from sira.knowledge_consolidation import KnowledgeConsolidationStore

CLAIM = 'Urban tree canopy can reduce daytime heat in neighborhoods by providing shade.'


class SelfModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sira-a82-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = SelfModelStore(self.root)
        self.git = patch('sira.self_model._git_head', return_value='abc1234')
        self.git.start()
        self.addCleanup(self.git.stop)
        self.provider = patch('sira.self_model.build_provider_health_snapshot',
                              return_value={'provider_count': 2, 'available_count': 1,
                                            'active_cooldown_count': 0, 'providers': [
                {'provider_id':'free_public','health':'ready','available_now':True,
                 'cost_class':'free','cooldown_active':False,'historical_failures':0},
                {'provider_id':'metered','health':'metered_budget_blocked',
                 'available_now':False,'cost_class':'metered','cooldown_active':False,
                 'historical_failures':0,'secret':'MUST_NOT_LEAK'}]})
        self.provider.start()
        self.addCleanup(self.provider.stop)

    def release(self, *, ready=True, head='abc1234'):
        path=self.root/'runtime/release'/('release_acceptance_'+'a'*32+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        report={'status':'passed' if ready else 'failed','release_ready':ready,
                'created_at':datetime.now(timezone.utc).isoformat(),
                'required_check_count':19,'required_checks_passed':19 if ready else 18,
                'git':{'available':True,'clean':True,'revision':head},
                'checks':{'engineering_canary_21_21':ready,
                          'protected_surface_unchanged':ready,
                          'source_surface_unchanged':ready,
                          'two_real_soaks_passed':ready,
                          'multicycle_real_soak_passed':ready}}
        path.write_text(json.dumps(report),encoding='utf-8')
        return path

    def cycle(self, identifier, outcome):
        path=self.root/'runtime/last_cycle.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({'kind':'self_improvement_cycle','cycle_id':identifier,
                'status':'completed','target_kind':'opportunity','outcome':outcome,
                'completed_at':datetime.now(timezone.utc).isoformat()}))

    def knowledge(self):
        store=KnowledgeConsolidationStore(self.root)
        now=datetime.now(timezone.utc).isoformat()
        for i,host in enumerate(('www.nasa.gov','www.noaa.gov')):
            store.record_evidence('claim.tree',CLAIM,evidence_id=f'e{i}',
                source_id=f's{i}',source_url=f'https://{host}/trees',confidence=.91,
                verifier_kind='independent_read',evidence_sha256=chr(97+i)*64,
                retrieved_at=now,verified=True)
        self.assertEqual(store.consolidate('claim.tree').status,'consolidated')

    def test_persists_across_reload_and_tracks_runtime_generation(self):
        RuntimeStateStore(self.root).save({'desired_state':'off','worker_state':'stopped','generation':64})
        first=self.store.refresh()
        second=SelfModelStore(self.root).load()
        self.assertEqual(first['identity']['name'],'SIRA')
        self.assertEqual(second['runtime']['generation'],64)
        self.assertEqual(second['runtime']['effective_state'],'stopped')
        self.assertEqual(second['identity']['head'],'abc1234')

    def test_code_and_config_alone_cannot_demonstrate_capability(self):
        model=self.store.refresh()
        for name in ('coding_engineering','research','skill_practice','self_improvement'):
            self.assertEqual(model['capabilities'][name]['state'],'unverified')
            self.assertEqual(model['capabilities'][name]['evidence'],[])
        self.assertFalse(model['release']['release_ready'])

    def test_passing_release_evidence_demonstrates_only_narrow_gates(self):
        path=self.release()
        model=self.store.refresh()
        for name in ('testing','verification'):
            row=model['capabilities'][name]
            self.assertEqual(row['state'],'demonstrated')
            self.assertEqual(row['scope'],'release_acceptance_and_canary')
            self.assertEqual(row['evidence'][0]['sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(model['capabilities']['coding_engineering']['state'],'unverified')

    def test_release_from_old_head_or_failed_gate_is_not_current_evidence(self):
        self.release(head='old1234')
        self.assertEqual(self.store.refresh()['capabilities']['testing']['state'],'unverified')
        self.release(ready=False)
        self.assertEqual(self.store.refresh()['capabilities']['testing']['state'],'unverified')

    def test_one_transient_candidate_rejection_preserves_demonstrated_testing(self):
        self.release()
        self.store.refresh()
        self.cycle('sc_'+'1'*32,'candidate_rejected')
        row=self.store.refresh()
        self.assertEqual(row['capabilities']['testing']['state'],'demonstrated')
        self.assertNotEqual(row['capabilities']['coding_engineering']['state'],'degraded')
        self.assertEqual(row['recent_failures'][0]['category'],'failed_candidate')

    def test_three_distinct_relevant_failures_degrade_and_create_gap(self):
        for i in range(3):
            self.cycle('sc_'+str(i)*32,'candidate_rejected')
            model=self.store.refresh()
        self.assertEqual(model['capabilities']['coding_engineering']['state'],'degraded')
        self.assertTrue(any(g['capability']=='coding_engineering' and g['reason']=='repeated_failures'
                            for g in capability_gaps(model)))
        self.assertEqual(len(model['recent_failures']),3)

    def test_duplicate_cycle_does_not_inflate_failure_count(self):
        self.cycle('sc_'+'1'*32,'candidate_rejected')
        for _ in range(4): model=self.store.refresh()
        self.assertEqual(len(model['recent_failures']),1)
        self.assertNotEqual(model['capabilities']['coding_engineering']['state'],'degraded')

    def test_later_success_can_recover_recent_failure_degradation(self):
        for i in range(3):
            self.cycle('sc_'+str(i)*32,'candidate_rejected')
            model=self.store.refresh()
        self.assertEqual(model['capabilities']['coding_engineering']['state'],'degraded')
        self.cycle('sc_'+'a'*32,'promotion_applied')
        model=self.store.refresh()
        self.assertNotEqual(model['capabilities']['coding_engineering']['state'],'degraded')
        self.assertEqual(len(model['recent_failures']),3)
        self.assertTrue(any(row['from']=='degraded' and row['capability']=='coding_engineering'
                            for row in model['history']))

    def test_provider_cooldown_is_reported_and_secret_is_not(self):
        with patch('sira.self_model.build_provider_health_snapshot',return_value={
                'provider_count':1,'available_count':0,'active_cooldown_count':1,
                'providers':[{'provider_id':'alpha','health':'provider_cooldown',
                              'available_now':False,'cost_class':'free',
                              'cooldown_active':True,'historical_failures':3,
                              'api_key':'SECRET-VALUE'}]}):
            model=self.store.refresh()
        self.assertEqual(model['providers']['cooling_count'],1)
        self.assertEqual(model['providers']['entries'][0]['state'],'cooling')
        self.assertNotIn('SECRET-VALUE',json.dumps(model))
        self.assertTrue(any(g['reason']=='provider_cooldown' for g in model['gaps']))

    def test_active_goals_are_bounded_without_full_records(self):
        LearningGoalStore(self.root).create('Python software testing',priority='high')
        model=self.store.refresh()
        self.assertEqual(model['learning']['active_goal_count'],1)
        self.assertEqual(model['learning']['goals'][0]['topic'],'Python software testing')
        self.assertNotIn('research_followups',model['learning']['goals'][0])

    def test_verified_knowledge_provenance_and_a79_local_hit_are_bounded(self):
        self.knowledge()
        model=self.store.refresh()
        self.assertEqual(model['knowledge']['verified_count'],1)
        self.assertEqual(model['capabilities']['memory_retrieval']['state'],'demonstrated')
        self.assertEqual(model['capabilities']['verified_learning']['state'],'demonstrated')
        self.assertLess(len(json.dumps(model)),100_000)
        self.assertFalse(model['knowledge']['bootstrap']['source_count'])

    def test_state_transitions_preserve_history_without_repeating_duplicates(self):
        self.store.refresh()
        self.release()
        demonstrated=self.store.refresh()
        size=len(demonstrated['history'])
        self.assertTrue(any(e['capability']=='testing' and e['to']=='demonstrated'
                            for e in demonstrated['history']))
        self.assertEqual(len(self.store.refresh()['history']),size)
        self.release(ready=False)
        changed=self.store.refresh()
        self.assertTrue(any(e['capability']=='testing' and e['from']=='demonstrated'
                            for e in changed['history']))

    def test_malformed_current_uses_previous_for_read_and_blocks_refresh(self):
        self.store.refresh()
        self.release()
        self.store.refresh()
        path=self.root/'memory/self_model/current.json'
        path.write_text('{broken',encoding='utf-8')
        recovered=self.store.load()
        self.assertEqual(recovered['integrity'],'recovered_previous')
        self.assertFalse(recovered['authority_granted'])
        with self.assertRaises(ValueError): self.store.refresh()
        self.assertEqual(path.read_text(encoding='utf-8'),'{broken')

    def test_model_is_not_authority_even_if_artifact_claims_it(self):
        path=self.release()
        data=json.loads(path.read_text())
        data.update(authority_granted=True,promotion_authorized=True,
                    paid_spending_authorized=True,skill_activated=True)
        path.write_text(json.dumps(data))
        model=self.store.refresh()
        for flag in ('authority_granted','promotion_authorized','paid_spending_authorized','skill_activated'):
            self.assertIs(model[flag],False)

    def test_gaps_identify_unverified_capabilities_and_are_bounded(self):
        model=self.store.refresh()
        gaps=capability_gaps(model,limit=4)
        self.assertLessEqual(len(gaps),4)
        self.assertTrue(any(g['reason']=='missing_evidence' for g in gaps))

    def test_read_only_collection_does_not_persist_or_call_network(self):
        snapshot=collect_self_model(self.root)
        self.assertFalse((self.root/'memory/self_model/current.json').exists())
        self.assertEqual((snapshot['resource_usage']['api_requests'],
                          snapshot['resource_usage']['model_requests']), (0,0))

    def test_recent_cycles_survive_reload_without_duplicate_history(self):
        self.cycle('sc_'+'a'*32, 'candidate_rejected')
        self.store.refresh()
        self.cycle('sc_'+'b'*32, 'sources_discovered_needs_verification')
        latest=self.store.refresh()
        self.assertEqual([r['cycle_id'] for r in latest['recent_activity']],
                         ['sc_'+'b'*32,'sc_'+'a'*32])
        self.assertEqual(len(SelfModelStore(self.root).refresh()['recent_activity']),2)

    def test_bootstrap_manifest_scan_is_bounded(self):
        folder=self.root/'memory/bootstrap_corpus/manifests'
        folder.mkdir(parents=True)
        for i in range(27): (folder/f'{i}.json').write_text('{}')
        model=self.store.refresh()
        self.assertEqual(model['knowledge']['bootstrap']['source_count'],24)
        self.assertEqual(model['knowledge']['bootstrap']['integrity'],'capacity_exceeded')

    def test_practice_file_presence_is_not_skill_demonstration(self):
        folder=self.root/'memory/learning_practice_verifications'
        folder.mkdir(parents=True)
        (folder/'untrusted.json').write_text('{"status":"verified"}')
        model=self.store.refresh()
        self.assertEqual(model['practice_artifacts']['learning_practice_verifications']
                         ['state'],'observed_unverified')
        self.assertEqual(model['capabilities']['skill_practice']['state'],'unverified')


if __name__=='__main__': unittest.main()
