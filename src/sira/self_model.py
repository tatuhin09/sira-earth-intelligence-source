"""A82: bounded, evidence-linked descriptive SIRA self-model.

This report is never an authorization input. Catalog presence and source code
alone do not demonstrate a capability. No network or model calls occur here.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from urllib.parse import quote

from .learning_goals import LearningGoalStore
from .knowledge_consolidation import KnowledgeConsolidationStore
from .memory_first import resolve_memory_first
from .models import utc_now
from .provider_health import build_provider_health_snapshot
from .provider_catalog import list_providers
from .provider_readiness import assess_provider
from .runtime import RuntimeStateStore
from .storage import write_json
from .advanced_evaluation import recent_independent_evidence
from .anti_gaming import recent_firewall_evidence

SCHEMA = "sira.self_model.v1"
MAX_RECORD_BYTES = 128_000
MAX_HISTORY = 96
MAX_FAILURES = 16
FAILURE_WINDOW_SECONDS = 72 * 3600
MAX_GOALS = 16
MAX_PROVIDERS = 24
MAX_WORKERS = 16
MAX_ACTIVITY = 16
MAX_RELEASE_ARTIFACTS = 32
MAX_RELEASE_SCAN = 4096
MAX_RELEASE_BYTES = 2_000_000
MAX_KNOWLEDGE_SAMPLE = 12
CAPABILITIES = (
    "research", "verified_learning", "memory_retrieval", "knowledge_freshness",
    "planning", "coding_engineering", "testing", "verification", "tool_use",
    "provider_routing", "failure_recovery", "long_horizon_execution",
    "skill_practice", "self_improvement",
)
STATES = {"demonstrated", "partially_demonstrated", "unverified", "degraded", "unavailable"}
_CYCLE_ID = re.compile(r"sc_[0-9a-f]{32}\Z")
_RELEASE_NAME = re.compile(r"release_acceptance_[0-9a-f]{32}\.json\Z")
_RESEARCH_VERIFIERS = frozenset({
    "general_exact_sentence_two_hosts_v1",
    "exact_sentence_two_public_documents_v1",
})
_RESEARCH_VERIFIER_PREFIXES = (
    "deterministic_joint_official_security_v1:",
    "deterministic_joint_official_scope_v1:",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe_json(path: Path, limit: int) -> tuple[dict, str] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        raw = path.read_bytes()
        if len(raw) > limit:
            return None
        value = json.loads(raw.decode("utf-8"))
        return (value, _sha(raw)) if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _git_head(root: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                            capture_output=True, text=True, timeout=3, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,40}", value) else None


def _release_evidence(root: Path, head: str | None, now: datetime) -> dict:
    directory = root / "runtime" / "release"
    unavailable = {"release_ready": False, "reason": "no_current_release_evidence", "evidence": []}
    if head is None or directory.is_symlink() or not directory.is_dir():
        return unavailable
    paths = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    paths.append(Path(entry.path))
                    if len(paths) > MAX_RELEASE_SCAN:
                        return {**unavailable, "reason": "release_scan_capacity_exceeded"}
    except OSError:
        return unavailable
    paths = sorted((p for p in paths if _RELEASE_NAME.fullmatch(p.name)),
                   key=lambda p: p.stat().st_mtime_ns if p.is_file() and not p.is_symlink() else 0,
                   reverse=True)[:MAX_RELEASE_ARTIFACTS]
    for path in paths:
        loaded = _safe_json(path, MAX_RELEASE_BYTES)
        if loaded is None:
            continue
        report, digest = loaded
        checks = report.get("checks")
        git = report.get("git")
        if (report.get("status") != "passed" or report.get("release_ready") is not True
                or not isinstance(git, dict) or git.get("revision") != head
                or git.get("clean") is not True or not isinstance(checks, dict)
                or any(checks.get(k) is not True for k in (
                    "engineering_canary_21_21", "protected_surface_unchanged",
                    "source_surface_unchanged", "two_real_soaks_passed",
                    "multicycle_real_soak_passed"))
                or type(report.get("required_check_count")) is not int
                or report["required_check_count"] < 19
                or report.get("required_checks_passed") != report["required_check_count"]):
            continue
        try:
            created = datetime.fromisoformat(str(report["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                continue
            age = (now - created.astimezone(timezone.utc)).total_seconds()
        except (KeyError, ValueError, AttributeError):
            continue
        if not 0 <= age <= 30 * 86400:
            continue
        ref = {"kind": "release_acceptance", "ref": path.name, "sha256": digest,
               "at": report["created_at"], "head": head}
        return {"release_ready": True, "reason": "current_head_release_gate_passed",
                "evidence": [ref], "required_check_count": report["required_check_count"],
                "required_checks_passed": report["required_checks_passed"]}
    return unavailable


def _knowledge(root: Path) -> tuple[dict, list[dict], bool, list[dict]]:
    info = {"verified_count": 0, "conflicted_count": 0, "stale_sample_count": 0,
            "sampled_count": 0, "bootstrap": {"source_count": 0, "candidate_count": 0,
                                         "verified_import_count": 0}, "integrity": "unavailable"}
    refs: list[dict] = []
    research_refs: list[dict] = []
    memory_hit = False
    db = root / "memory" / "sira_knowledge.sqlite3"
    if db.is_symlink():
        info["integrity"] = "unsafe_path"
        return info, refs, memory_hit, research_refs
    if db.is_file():
        try:
            uri = "file:" + quote(str(db.resolve()), safe="/") + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                info["verified_count"] = conn.execute(
                    "SELECT COUNT(*) FROM consolidated_knowledge WHERE status='active'").fetchone()[0]
                info["conflicted_count"] = conn.execute(
                    "SELECT COUNT(*) FROM consolidated_knowledge WHERE status='conflicted'").fetchone()[0]
                info["bootstrap"]["verified_import_count"] = conn.execute(
                    "SELECT COUNT(DISTINCT knowledge_key) FROM knowledge_evidence WHERE verifier_kind='bootstrap_exact_excerpt_two_host_v1'").fetchone()[0]
                rows = conn.execute("""SELECT knowledge_key FROM consolidated_knowledge
                    WHERE status='active' ORDER BY last_verified_at DESC LIMIT ?""",
                    (MAX_KNOWLEDGE_SAMPLE,)).fetchall()
            store = KnowledgeConsolidationStore(root)
            for item in rows:
                row = store.lookup(item["knowledge_key"])
                if row is None:
                    continue
                info["sampled_count"] += 1
                info["stale_sample_count"] += int(row["freshness"] == "stale")
                if row["freshness"] != "fresh" or len(refs) >= 2:
                    continue
                result = resolve_memory_first(root, str(row["claim_text"]))
                if result["status"] != "memory_resolved":
                    continue
                for hit in result["results"]:
                    if hit["knowledge_key"] == row["knowledge_key"]:
                        source_ids = [str(p["source_id"])[:128] for p in hit["provenance"][:4]]
                        refs.append({"kind": "verified_memory_hit", "ref": str(row["knowledge_key"]),
                                     "source_ids": source_ids, "at": str(row["last_verified_at"])})
                        research_sources = [p for p in hit["provenance"]
                                            if p["verifier_kind"] in _RESEARCH_VERIFIERS or
                                            p["verifier_kind"].startswith(_RESEARCH_VERIFIER_PREFIXES)]
                        if len({p["source_host"] for p in research_sources}) >= 2:
                            research_refs.append({"kind": "verified_research_claim",
                                "ref": str(row["knowledge_key"]),
                                "source_ids": [str(p["source_id"])[:128]
                                               for p in research_sources[:4]],
                                "at": str(row["last_verified_at"])})
                        memory_hit = True
                        break
            info["integrity"] = "readable"
        except (sqlite3.DatabaseError, ValueError, OSError):
            info["integrity"] = "unreadable"
            info["verified_count"] = 0
            refs = []
            research_refs = []
            memory_hit = False
    base = root / "memory" / "bootstrap_corpus"
    for section, target, cap in (("manifests", "source_count", 24),
                                 ("candidates", "candidate_count", 192)):
        folder = base / section
        if folder.is_symlink() or base.is_symlink():
            info["bootstrap"][target] = 0
            info["bootstrap"]["integrity"] = "unsafe_path"
        elif folder.is_dir():
            count = 0
            try:
                with os.scandir(folder) as entries:
                    for row in entries:
                        if row.name.endswith('.json') and count_guard(row):
                            count += 1
                        if count > cap:
                            break
            except OSError:
                info['bootstrap']['integrity'] = 'unreadable'
            info["bootstrap"][target] = min(count, cap)
            if count > cap:
                info["bootstrap"]["integrity"] = "capacity_exceeded"
    return info, refs, memory_hit, research_refs


def count_guard(entry) -> bool:
    return not entry.is_symlink() and entry.is_file()


def _goals(root: Path) -> dict:
    try:
        goals = LearningGoalStore(root).list()
        active = [row for row in goals if row.get("status") == "active"]
        return {"active_goal_count": len(active), "goals": [
            {"goal_id": str(row.get("goal_id"))[:50],
             "topic": str(row.get("topic"))[:120], "priority": str(row.get("priority"))[:16]}
            for row in active[:MAX_GOALS]], "truncated": len(active) > MAX_GOALS}
    except (OSError, ValueError):
        return {"active_goal_count": 0, "goals": [], "integrity": "unreadable"}


def _providers(root: Path) -> dict:
    try:
        raw = build_provider_health_snapshot(root, allow_metered=False)
    except (OSError, ValueError, KeyError, TypeError):
        return {"known_count": 0, "available_count": 0, "cooling_count": 0,
                "entries": [], "integrity": "unavailable"}
    rows = raw.get("providers") or []
    if not isinstance(rows, list):
        rows = []
    entries = []
    catalog = {item.provider_id: item for item in list_providers()}
    for row in rows[:MAX_PROVIDERS]:
        if not isinstance(row, dict):
            continue
        health = str(row.get("health") or "unknown")[:40]
        provider_id = str(row.get("provider_id") or "unknown")[:80]
        descriptor = catalog.get(provider_id)
        readiness = assess_provider(descriptor) if descriptor else None
        failures = row.get('historical_failures')
        failures = failures if type(failures) is int and failures >= 0 else 0
        entries.append({"provider_id": str(row.get("provider_id") or "unknown")[:80],
                        "state": "cooling" if row.get("cooldown_active") is True else
                                 "degraded" if health == "degraded_history" else
                                 "available" if health == "ready" and row.get("available_now") is True else
                                 "unavailable",
                        "health": health, "cost_class": str(row.get("cost_class") or "unknown")[:16],
                        "credential_state": ('observed_in_process' if readiness.credential_configured else
                                             'not_observed_in_process') if readiness else 'not_observed_in_snapshot',
                        "task_classes": list(descriptor.capabilities[:8]) if descriptor else [],
                        "historical_failures": failures})
    return {"known_count": min(int(raw.get("provider_count") or 0), 1000),
            "available_count": min(int(raw.get("available_count") or 0), 1000),
            "cooling_count": min(int(raw.get("active_cooldown_count") or 0), 1000),
            "entries": entries, "truncated": len(rows) > MAX_PROVIDERS,
            "integrity": "readable"}


def _workers(root: Path, runtime: dict) -> dict:
    rows = []
    folder = root / "improvements" / "workers" / "tasks"
    if folder.is_dir() and not folder.is_symlink():
        try:
            paths = []
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.endswith(".json"):
                        paths.append(Path(entry.path))
                        if len(paths) > 64:
                            return {"runtime_worker": runtime.get("worker_state"),
                                    "active_cycle": runtime.get("active_cycle"),
                                    "recent_tasks": [], "integrity": "scan_capacity_exceeded"}
            for path in sorted(paths, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:MAX_WORKERS]:
                loaded = _safe_json(path, 64_000)
                if loaded is None:
                    continue
                row, _ = loaded
                rows.append({"task_id": str(row.get("task_id"))[:50],
                             "status": str(row.get("status"))[:40],
                             "roles": [str(role)[:32] for role in list((row.get("workers") or {}))[:8]]})
        except (OSError, TypeError):
            rows = []
    active = runtime.get("active_cycle")
    return {"runtime_worker": runtime.get("worker_state"),
            "active_cycle": {"cycle_id": active.get("cycle_id"), "phase": active.get("phase")}
                             if isinstance(active, dict) else None,
            "recent_tasks": rows}


def _resources(root: Path) -> dict:
    usage = shutil.disk_usage(root)
    available_ram = None
    try:
        for line in Path('/proc/meminfo').read_text(encoding='ascii').splitlines()[:8]:
            if line.startswith('MemAvailable:'):
                available_ram = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError):
        pass
    return {"disk_total_bytes": usage.total, "disk_free_bytes": usage.free,
            "memory_available_bytes": available_ram, "cpu_count": os.cpu_count()}


def _practice_artifacts(root: Path) -> dict:
    """Count bounded artifacts without accepting their claims as verification."""
    output = {}
    for name in ('learning_practice_runs', 'learning_practice_verifications',
                 'learning_skill_lifecycle'):
        folder = root / 'memory' / name
        count = 0
        state = 'empty'
        if folder.is_symlink():
            state = 'unsafe_path'
        elif folder.is_dir():
            state = 'observed_unverified'
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if entry.name.endswith('.json') and entry.is_file(follow_symlinks=False):
                            count += 1
                        if count > 64:
                            state = 'capacity_exceeded'
                            break
            except OSError:
                state = 'unreadable'
        output[name] = {'observed_top_level_count': min(count, 64), 'state': state}
    return output


def _failure(outcome: str) -> tuple[str, tuple[str, ...]] | None:
    if any(token in outcome for token in ('quota', 'cooldown', 'budget_exhausted')):
        return 'quota_cooldown', ('provider_routing', 'research')
    if any(token in outcome for token in ('network', 'timeout', 'provider_unavailable')):
        return 'provider_network', ('provider_routing', 'research')
    if any(token in outcome for token in ('verification_failed', 'evaluator_rejected')):
        return 'verification_failure', ('verification', 'coding_engineering')
    if any(token in outcome for token in ('candidate_rejected', 'structural_goal_not_met')):
        return 'failed_candidate', ('coding_engineering', 'self_improvement')
    if any(token in outcome for token in ('worker_failed', 'runtime_failed')):
        return 'transient_runtime', ('long_horizon_execution', 'failure_recovery')
    if 'unsupported' in outcome:
        return 'unsupported_capability', ('tool_use',)
    return None


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _recovered_by(activity: list[dict], capability: str, after: datetime) -> bool:
    successful = {
        'coding_engineering': {'promotion_applied'},
        'self_improvement': {'promotion_applied'},
        'research': {'verified_knowledge_recorded'},
        'verified_learning': {'verified_knowledge_recorded'},
    }.get(capability, set())
    return any(row.get('outcome') in successful and
               (at := _timestamp(row.get('completed_at'))) is not None and at > after
               for row in activity)


def capability_gaps(model: dict, *, limit: int = 12) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 24:
        raise ValueError('Gap limit must be 1..24')
    gaps = []
    for row in model['providers']['entries']:
        if row['state'] == 'cooling':
            gaps.append({'capability': 'provider:' + row['provider_id'],
                         'reason': 'provider_cooldown', 'state': 'degraded',
                         'evidence_refs': ['provider_health:' + row['provider_id']]})
    for name in CAPABILITIES:
        row = model['capabilities'][name]
        if row['state'] == 'degraded':
            gaps.append({'capability': name, 'reason': 'repeated_failures',
                         'state': 'degraded', 'evidence_refs': [e['ref'] for e in row['evidence'][:2]]})
    for name in CAPABILITIES:
        row = model['capabilities'][name]
        if row['state'] not in {'demonstrated', 'degraded'}:
            gaps.append({'capability': name,
                         'reason': 'missing_evidence' if not row['evidence'] else 'partial_evidence',
                         'state': row['state'], 'evidence_refs': [e['ref'] for e in row['evidence'][:2]]})
    return gaps[:limit]


def collect_self_model(root: Path, *, previous: dict | None = None) -> dict:
    root = Path(root).resolve()
    now = datetime.now(timezone.utc)
    head = _git_head(root)
    runtime = RuntimeStateStore(root).status()
    release = _release_evidence(root, head, now)
    knowledge, knowledge_refs, memory_hit, research_refs = _knowledge(root)
    goals = _goals(root)
    providers = _providers(root)
    workers = _workers(root, runtime)
    previous_failures = list(previous.get('recent_failures') or []) if previous else []
    cycle = runtime.get('last_cycle') or {}
    cycle_id, outcome = cycle.get('cycle_id'), cycle.get('outcome')
    classified = _failure(outcome) if isinstance(outcome, str) else None
    if (classified is not None and isinstance(cycle_id, str)
            and _CYCLE_ID.fullmatch(cycle_id)
            and cycle_id not in {row.get('cycle_id') for row in previous_failures}):
        category, affected = classified
        previous_failures.insert(0, {'cycle_id': cycle_id, 'outcome': outcome[:80],
            'category': category, 'affected_capabilities': list(affected),
            'at': cycle.get('completed_at')})
    failures = previous_failures[:MAX_FAILURES]
    recent_activity = list(previous.get('recent_activity') or []) if previous else []
    if (isinstance(cycle_id, str) and _CYCLE_ID.fullmatch(cycle_id)
            and cycle_id not in {row.get('cycle_id') for row in recent_activity}):
        recent_activity.insert(0, {key: cycle.get(key) for key in (
            'cycle_id', 'target_kind', 'outcome', 'experiment_id', 'promotion_id',
            'handoff_id', 'completed_at')})
    recent_activity = recent_activity[:MAX_ACTIVITY]
    capabilities = {name: {'state': 'unverified', 'scope': 'no_current_demonstration',
                            'reason': 'missing_independent_evidence', 'evidence': []}
                    for name in CAPABILITIES}
    def mark(name, state, scope, reason, evidence):
        capabilities[name] = {'state': state, 'scope': scope, 'reason': reason,
                              'evidence': evidence[:3]}
    if release['release_ready']:
        for name in ('testing', 'verification'):
            mark(name, 'demonstrated', 'release_acceptance_and_canary',
                 'current_head_protected_gate', release['evidence'])
        mark('tool_use', 'partially_demonstrated', 'bounded_coordination_canary',
             'release_canary_is_not_general_tool_execution', release['evidence'])
        mark('failure_recovery', 'partially_demonstrated', 'release_reliability_diagnostic',
             'diagnostic_not_long_duration_proof', release['evidence'])
        mark('long_horizon_execution', 'partially_demonstrated', 'bounded_multicycle_soak',
             'bounded_soak_not_multiday_proof', release['evidence'])
    if knowledge_refs and knowledge['integrity'] == 'readable':
        mark('verified_learning', 'demonstrated', 'narrow_provenance_bound_claims',
             'current_independent_verified_claims', knowledge_refs)
        if memory_hit:
            mark('memory_retrieval', 'demonstrated', 'bounded_local_a79_resolution',
                 'fresh_current_claim_resolved_locally', knowledge_refs)
    if research_refs and knowledge['integrity'] == 'readable':
        mark('research', 'partially_demonstrated', 'bounded_two_host_source_research',
             'verified_claim_evidence_not_general_research_mastery', research_refs)
    else:
        capabilities['research']['reason'] = 'no_current_two_host_verified_research_claim'
    if knowledge['stale_sample_count'] or knowledge['conflicted_count']:
        mark('knowledge_freshness', 'partially_demonstrated', 'detected_lifecycle_gap',
             'freshness_or_conflict_detected_not_revalidated', knowledge_refs)
    if providers['available_count'] > 0:
        mark('provider_routing', 'partially_demonstrated', 'read_only_policy_and_health',
             'availability_is_not_successful_provider_call', [{
                 'kind':'provider_health','ref':'provider_health:local','at':utc_now()}])
    independent_refs = recent_independent_evidence(root)
    if independent_refs:
        # A86 integrity evidence is descriptive context only: it never changes
        # the state or scope, and A85 evidence stays first.
        mark('coding_engineering', 'partially_demonstrated',
             'bounded_versioned_independent_suite',
             'independent_suite_pass_not_general_engineering_mastery',
             independent_refs[:2] + recent_firewall_evidence(root, limit=1))
    for name in CAPABILITIES:
        relevant = [row for row in failures if name in row.get('affected_capabilities', ())
                    and (at := _timestamp(row.get('at'))) is not None
                    and 0 <= (now - at).total_seconds() <= FAILURE_WINDOW_SECONDS]
        if len(relevant) >= 3:
            most_recent = max(_timestamp(row['at']) for row in relevant)
            if _recovered_by(recent_activity, name, most_recent):
                continue
            old = capabilities[name]
            mark(name, 'degraded', old['scope'], 'three_distinct_relevant_failures',
                 old['evidence'] + [{'kind':'runtime_failure','ref':row['cycle_id'],
                                     'at':row.get('at')} for row in relevant[:3]])
    result = {
        'schema': SCHEMA, 'created_at': utc_now(),
        'identity': {'name':'SIRA','schema_version':1,'head':head,
                     'runtime_stage':runtime.get('sira_runtime_stage')},
        'runtime': {key:runtime.get(key) for key in (
            'generation','desired_state','effective_state','worker_state','worker_alive',
            'state_health','last_cycle')},
        'release': release, 'capabilities': capabilities,
        'providers': providers,
        'models': {'configured_classes': [], 'availability': 'not_independently_probed'},
        'tools': {'capability_broker': 'registered_not_authority',
                  'execution_availability': 'not_probed'},
        'workers': workers, 'knowledge': knowledge, 'learning': goals,
        'practice_artifacts': _practice_artifacts(root),
        'recent_activity': recent_activity,
        'recent_improvements': [row for row in recent_activity
                                if row.get('promotion_id') and row.get('outcome') == 'promotion_applied'][:8],
        'recent_failures': failures,
        'resources': _resources(root),
        'resource_usage': {'api_requests':0,'model_requests':0,'paid_requests':0},
        'authority_granted':False,'promotion_authorized':False,
        'paid_spending_authorized':False,'skill_activated':False,
    }
    result['gaps'] = capability_gaps(result)
    return result


class SelfModelStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.directory = self.root / 'memory' / 'self_model'
        self.current = self.directory / 'current.json'
        self.previous = self.directory / 'previous.json'
        self.lock = self.directory / '.lock'

    def _validated(self, path: Path) -> dict | None:
        loaded = _safe_json(path, MAX_RECORD_BYTES)
        if loaded is None:
            return None
        row, _ = loaded
        digest = row.pop('record_sha256', None)
        if (row.get('schema') != SCHEMA or digest != _sha(_canonical(row))
                or any(row.get(flag) is not False for flag in (
                    'authority_granted','promotion_authorized',
                    'paid_spending_authorized','skill_activated'))
                or not isinstance(row.get('capabilities'), dict)
                or set(row['capabilities']) != set(CAPABILITIES)
                or not isinstance(row.get('history'), list)
                or len(row['history']) > MAX_HISTORY
                or not isinstance(row.get('recent_activity'), list)
                or len(row['recent_activity']) > MAX_ACTIVITY
                or not isinstance(row.get('recent_failures'), list)
                or len(row['recent_failures']) > MAX_FAILURES):
            return None
        for name in CAPABILITIES:
            entry = row['capabilities'][name]
            if (not isinstance(entry, dict) or entry.get('state') not in STATES
                    or not isinstance(entry.get('evidence'), list)
                    or len(entry['evidence']) > 3):
                return None
        row['record_sha256'] = digest
        return row

    def load(self) -> dict | None:
        if self.directory.is_symlink() or self.current.is_symlink() or self.previous.is_symlink():
            raise ValueError('Unsafe self-model path')
        current = self._validated(self.current)
        if current is not None:
            return {**current, 'integrity':'ok'}
        backup = self._validated(self.previous)
        if backup is not None:
            return {**backup, 'integrity':'recovered_previous'}
        if self.current.exists() or self.previous.exists():
            raise ValueError('No valid self-model snapshot')
        return None

    @contextmanager
    def _locked(self):
        if (self.directory.is_symlink() or self.lock.is_symlink()
                or self.current.is_symlink() or self.previous.is_symlink()):
            raise ValueError('Unsafe self-model path')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def refresh(self) -> dict:
        with self._locked():
            existing = self.load()
            if self.current.exists() and (existing is None or existing.get('integrity') != 'ok'):
                raise ValueError('Malformed current self-model; previous snapshot retained')
            result = collect_self_model(self.root, previous=existing)
            history = list(existing.get('history') or []) if existing else []
            dropped = int(existing.get('history_dropped') or 0) if existing else 0
            for name in CAPABILITIES:
                before = (existing.get('capabilities') or {}).get(name, {}).get('state', 'unverified') if existing else 'unverified'
                after = result['capabilities'][name]['state']
                if before != after:
                    history.append({'capability':name,'from':before,'to':after,
                                    'at':result['created_at'],
                                    'reason':result['capabilities'][name]['reason'],
                                    'evidence_refs':[e['ref'] for e in result['capabilities'][name]['evidence'][:3]]})
            if len(history) > MAX_HISTORY:
                dropped += len(history) - MAX_HISTORY
                history = history[-MAX_HISTORY:]
            result['history'], result['history_dropped'] = history, dropped
            result['record_sha256'] = _sha(_canonical(result))
            if len(_canonical(result)) > MAX_RECORD_BYTES:
                raise ValueError('Self-model snapshot exceeds bound')
            if self.current.is_file():
                os.replace(self.current, self.previous)
            write_json(self.current, result)
            self.current.chmod(0o600)
            return {**result, 'integrity':'ok'}
