"""A83: one bounded, descriptive SIRA dispatch state over proven subsystems.

Research jobs are queued under their existing guard; learning and engineering
requests are referrals to existing pipelines. Nothing in this module grants
capability broker, runtime, evaluator or promotion authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .desktop_research import DesktopResearchJobStore, create_research_job
from .learning_goals import LearningGoalStore
from .memory_first import resolve_memory_first
from .models import utc_now
from .self_model import SelfModelStore, collect_self_model
from .storage import write_json

SCHEMA='sira.core_tasks.v1'
INTENTS=frozenset({'answer_from_memory','research','learn','inspect_self','engineering','general'})
MAX_HISTORY=64
MAX_BYTES=100_000

class CoreStateError(ValueError):
    """Persistent Core state cannot safely be used."""


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),
                              ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()

class _TaskStore:
    def __init__(self, root: Path):
        self.directory=root/'runtime/core'
        self.path=self.directory/'tasks.json'
        self.backup=self.directory/'previous.json'
        self.lock=self.directory/'.lock'

    def _read(self, path: Path) -> dict | None:
        if path.is_symlink(): raise CoreStateError('Unsafe Core state path')
        if not path.exists(): return None
        try:
            if path.stat().st_size>MAX_BYTES: raise CoreStateError('Core state too large')
            data=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,UnicodeError,ValueError) as exc:
            raise CoreStateError('Malformed Core state') from exc
        if not isinstance(data,dict): raise CoreStateError('Malformed Core state')
        digest=data.pop('sha256',None)
        valid=(set(data)=={'schema','tasks','dropped'}
               and data.get('schema')==SCHEMA and digest==_digest(data)
               and isinstance(data.get('tasks'),list)
               and len(data['tasks'])<=MAX_HISTORY
               and type(data.get('dropped')) is int and data['dropped']>=0)
        if not valid: raise CoreStateError('Core state integrity mismatch')
        for task in data['tasks']:
            if (not isinstance(task,dict) or task.get('intent') not in INTENTS
                or task.get('status') not in {'pending','completed','failed','blocked'}
                or task.get('origin') not in {'user','background','system'}
                or not isinstance(task.get('task_id'),str)
                or not re.fullmatch(r'ct_[0-9a-f]{32}',task['task_id'])
                or any(task.get(key) is not False for key in (
                    'authority_granted','promotion_performed','skill_activated','paid_spending'))
                or not isinstance(task.get('artifact_refs'),list)
                or len(task['artifact_refs'])>2
                or not isinstance(task.get('evidence_refs'),list)
                or len(task['evidence_refs'])>2):
                raise CoreStateError('Malformed Core task')
        data['sha256']=digest
        return data

    def load(self) -> dict:
        if self.directory.is_symlink() or self.backup.is_symlink():
            raise CoreStateError('Unsafe Core directory')
        # A corrupted current snapshot is never silently replaced by a backup.
        current=self._read(self.path)
        if current is not None: return current
        previous=self._read(self.backup)
        return previous or {'schema':SCHEMA,'tasks':[],'dropped':0}

    @contextmanager
    def locked(self):
        if self.directory.is_symlink() or self.path.is_symlink() or self.lock.is_symlink():
            raise CoreStateError('Unsafe Core directory')
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)

    def save(self, state: dict) -> None:
        value={k:v for k,v in state.items() if k!='sha256'}
        if len(value['tasks'])>MAX_HISTORY:
            extra=len(value['tasks'])-MAX_HISTORY
            value['tasks']=value['tasks'][-MAX_HISTORY:]
            value['dropped']+=extra
        value['sha256']=_digest(value)
        if len(json.dumps(value,ensure_ascii=False).encode('utf-8'))>MAX_BYTES:
            raise CoreStateError('Core history exceeds bound')
        if self.path.exists():
            os.replace(self.path,self.backup)
        write_json(self.path,value)
        self.path.chmod(0o600)


class SiraCore:
    """Read shared SIRA state and dispatch only bounded, explicitly named intents."""
    def __init__(self, root: Path):
        self.root=Path(root).resolve()
        self.tasks=_TaskStore(self.root)

    def _model(self) -> dict:
        previous=SelfModelStore(self.root).load()
        model=collect_self_model(self.root,previous=previous)
        if any(model.get(k) is not False for k in (
            'authority_granted','promotion_authorized','paid_spending_authorized','skill_activated')):
            raise CoreStateError('Self-model cannot confer authority')
        return model

    @staticmethod
    def _priority(task: dict) -> int:
        if task['origin']=='user': return 1
        if task['origin']=='system': return 2
        if task['intent']=='engineering': return 4
        return 5

    def _view(self, model: dict, stored: dict) -> dict:
        tasks=stored['tasks']
        pending=sorted((t for t in tasks if t['status']=='pending'),
                       key=lambda t:(self._priority(t),t['received_at'],t['task_id']))
        user=next((t for t in pending if t['origin']=='user'),None)
        runtime=model['runtime']
        return {'schema':'sira.core_state.v1','identity':model['identity'],
                'runtime':runtime,'release':model['release'],
                'current_direct_user_task':user['task_id'] if user else None,
                'pending_work':pending[:16],'task_history':tasks[-MAX_HISTORY:],
                'active_goals':model['learning'],'verified_memory':model['knowledge'],
                'capability_gaps':model['gaps'][:12],
                'providers':model['providers'],'workers':model['workers'],
                'recent_outcome':(runtime.get('last_cycle') or {}).get('outcome'),
                'self_model':model,'preemption':{
                    'policy':'finish_or_cancel_bounded_current_operation' if user else 'none',
                    'transactional_promotion_interrupted':False,
                    'runtime_active_cycle':model['workers'].get('active_cycle')},
                'authority_granted':False,'promotion_authorized':False,
                'paid_spending_authorized':False,'skill_activated':False,
                'resource_usage':{'api_requests':0,'model_requests':0,'paid_requests':0}}

    def snapshot(self) -> dict:
        return self._view(self._model(),self.tasks.load())

    def reconcile_research_jobs(self) -> int:
        """Reflect persisted desktop execution, never certify research evidence."""
        history=self.tasks.load()['tasks']
        pending=[row['artifact_refs'][0] for row in history
                 if row.get('route')=='research_job' and row.get('status')=='pending'
                 and len(row.get('artifact_refs',[]))==1]
        jobs=DesktopResearchJobStore(self.root)
        updated=0
        for job_id in pending[:MAX_HISTORY]:
            if not re.fullmatch(r'dr_[0-9a-f]{32}',job_id):
                continue
            job=jobs.read(job_id)
            if not job or job.get('job_id')!=job_id:
                continue
            mapped={'completed':'completed','failed':'failed','blocked':'blocked',
                    'limited':'blocked','cancelled':'blocked'}.get(job.get('status'))
            if mapped is None:
                continue
            verification=job.get('verification') or {}
            proof=(verification.get('status') if isinstance(verification,dict) else None)
            proof=proof[:80] if isinstance(proof,str) else 'not_verified'
            with self.tasks.locked():
                stored=self.tasks.load()
                changed=False
                for task in stored['tasks']:
                    if (task.get('route')=='research_job' and task.get('status')=='pending'
                        and task.get('artifact_refs')==[job_id]):
                        task.update(status=mapped,verification_status=proof,
                                    reason='research_job_'+str(job['status'])[:40])
                        updated+=1
                        changed=True
                if changed:
                    self.tasks.save(stored)
        return updated

    def dispatch(self, intent: str, *, text: str | None=None, goal_id: str | None=None,
                 origin: str='user', allow_research: bool=False,
                 allow_paid: bool=False) -> dict:
        if intent not in INTENTS or origin not in {'user','background','system'}:
            raise ValueError('Unsupported bounded Core intent or origin')
        if type(allow_research) is not bool or type(allow_paid) is not bool or allow_paid:
            raise ValueError('Paid or invalid Core request is not authorized')
        if text is not None and (not isinstance(text,str) or not 1<=len(text.strip())<=500):
            raise ValueError('Core task input must be bounded text')
        if intent in {'answer_from_memory','research','engineering'} and not text:
            raise ValueError('Task text required')
        if intent=='learn':
            if not isinstance(goal_id,str): raise ValueError('Goal id required')
            goal=LearningGoalStore(self.root).get(goal_id)
            if goal['status']!='active': raise ValueError('Learning goal not active')
        model=None
        integrity_issue=False
        try:
            model=self._model()
        except CoreStateError:
            # Fail closed without accepting the forged self-model as authority.
            integrity_issue=True
            model=collect_self_model(self.root)
        if model['identity']['name']!='SIRA':
            raise CoreStateError('SIRA identity mismatch')
        safe_text=' '.join(text.split()) if text else None
        with self.tasks.locked():
            stored=self.tasks.load()  # fail before creating any research job
            task={'task_id':'ct_'+uuid4().hex,'intent':intent,'origin':origin,
                  'received_at':utc_now(),'input_sha256':hashlib.sha256(
                      (safe_text or goal_id or intent).encode()).hexdigest(),
                  'goal_id':goal_id if intent=='learn' else None,
                  'priority':1 if origin=='user' else 2 if origin=='system' else
                             4 if intent=='engineering' else 5,
                  'status':'pending','route':'pending','reason':'bounded_dispatch',
                  'verification_status':'not_verified','artifact_refs':[],
                  'evidence_refs':[],'authority_granted':False,
                  'promotion_performed':False,'skill_activated':False,
                  'paid_spending':False}
            stored['tasks'].append(task)
            self.tasks.save(stored)
        memory=None
        result=None
        try:
            if integrity_issue:
                task.update(status='blocked',route='self_model_integrity_blocked',
                            reason='self_model_authority_violation')
            elif intent=='inspect_self':
                task.update(status='completed',route='self_status',reason='read_only_snapshot')
            elif intent=='learn':
                task.update(route='existing_learning_pipeline',reason='owner_goal_pending_existing_gates',
                            artifact_refs=[goal_id])
            elif intent=='engineering':
                task.update(route='protected_engineering_pipeline',reason='await_existing_evaluation_and_authorization')
            elif intent=='general':
                task.update(status='blocked',route='bounded_fallback',reason='no_named_safe_subsystem')
            else:
                memory=resolve_memory_first(self.root,safe_text,
                       request_fresh_research=intent=='research')
                if memory['status']=='memory_resolved':
                    task.update(status='completed',route='verified_memory',
                                reason='fresh_independent_verified_claim',
                                verification_status='memory_preverified',
                                evidence_refs=[r['knowledge_key'] for r in memory['results'][:2]])
                    result=memory
                elif allow_research:
                    # Queue only. Existing research settings/provider guards remain in charge of execution.
                    job=create_research_job(self.root,safe_text)
                    task.update(route='research_job',reason=memory['reason'],
                                artifact_refs=[job['job_id']])
                else:
                    task.update(status='blocked',route='research_needed',reason=memory['reason'])
        except Exception as exc:
            task.update(status='failed',route='failed_closed',reason=type(exc).__name__)
            with self.tasks.locked():
                stored=self.tasks.load()
                for row in stored['tasks']:
                    if row['task_id']==task['task_id']: row.update(task)
                self.tasks.save(stored)
            raise
        with self.tasks.locked():
            stored=self.tasks.load()
            for row in stored['tasks']:
                if row['task_id']==task['task_id']: row.update(task)
            self.tasks.save(stored)
        if integrity_issue:
            # Never echo or use forged self-model content in a Core response.
            return {'schema':'sira.core_dispatch.v1','identity':{'name':'SIRA'},
                    'route':task['route'],'task':task,'state':{'status':'blocked'},
                    'result':None,'memory_decision':None,
                    'resource_usage':{'api_requests':0,'model_requests':0,'paid_requests':0},
                    'authority_granted':False,'promotion_authorized':False,
                    'skill_activated':False}
        view=self.snapshot()
        return {'schema':'sira.core_dispatch.v1','identity':view['identity'],
                'route':task['route'],'task':task,'state':view,
                'result':result,'memory_decision':memory,
                'resource_usage':{'api_requests':0,'model_requests':0,'paid_requests':0},
                'authority_granted':False,'promotion_authorized':False,
                'skill_activated':False}
