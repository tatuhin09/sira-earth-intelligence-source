"""A80: bounded, provenance-preserving lifecycle for consolidated knowledge.

Old schema-v1 claims have an implicit legacy interval until explicitly assigned
one of the four policies. This module never fetches sources or activates skills.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Mapping
from uuid import uuid4

from .knowledge_consolidation import (FRESHNESS_POLICIES, KnowledgeConsolidationStore,
                                      MIN_CONFIDENCE, _claim, _confidence, _evidence_id,
                                      _key, _sha, _source, _timestamp)
from .models import utc_now

MAX_EVENTS_PER_KEY = 32
MAX_REFS_PER_TRANSITION = 64
_REASON = re.compile(r"[\w .,:;()/-]{3,200}\Z", re.UNICODE)


def _reason(value: str) -> str:
    if not isinstance(value, str) or not _REASON.fullmatch(value):
        raise ValueError('A bounded transition reason is required')
    return value


def _topic_terms(value: str) -> set[str]:
    return set(re.findall(r"\w+", value.casefold())) - {
        'a', 'an', 'and', 'are', 'by', 'for', 'in', 'is', 'of', 'on', 'the', 'to', 'with',
    }


def _opposition(first: str, second: str) -> bool:
    def polarity(value):
        tokens = re.findall(r"\w+", value.casefold())
        return ([t for t in tokens if t not in {'no', 'not', 'never'}],
                any(t in {'no', 'not', 'never'} for t in tokens))
    left, left_neg = polarity(first)
    right, right_neg = polarity(second)
    return left == right and left_neg != right_neg


class KnowledgeEvolutionStore:
    def __init__(self, root: Path):
        self.store = KnowledgeConsolidationStore(root)

    def _references(self, conn, keys: tuple[str, ...]) -> list[dict]:
        refs = []
        for key in keys:
            rows = conn.execute('''SELECT evidence_id,source_id,source_url,source_host,
                evidence_sha256,claim_hash FROM knowledge_evidence WHERE knowledge_key=?
                ORDER BY recorded_at,evidence_id LIMIT ?''', (key, MAX_REFS_PER_TRANSITION + 1)).fetchall()
            if len(rows) > MAX_REFS_PER_TRANSITION or len(refs) + len(rows) > MAX_REFS_PER_TRANSITION:
                raise ValueError('Too many evidence references for bounded transition')
            refs.extend([{'knowledge_key': key, **dict(row)} for row in rows])
        if not refs:
            raise ValueError('Verified evidence required for lifecycle transition')
        return refs

    def _current(self, conn, key: str) -> dict:
        row = conn.execute('''SELECT c.*,l.freshness_class,l.policy_days,l.state,
            l.successor_key,l.reason,l.transitioned_at FROM consolidated_knowledge c
            LEFT JOIN knowledge_lifecycle l ON l.knowledge_key=c.knowledge_key
            WHERE c.knowledge_key=?''', (key,)).fetchone()
        if row is None:
            raise ValueError('Consolidated knowledge is required')
        data = dict(row)
        if data['status'] != 'active' or data['state'] not in (None, 'current'):
            raise ValueError('Knowledge is not current and active')
        if data['state'] is not None and (
                data['freshness_class'] not in FRESHNESS_POLICIES or
                type(data['policy_days']) is not int or
                data['policy_days'] != FRESHNESS_POLICIES[data['freshness_class']] or
                data['successor_key'] is not None or
                not isinstance(data['reason'], str) or
                not 1 <= len(data['reason']) <= 200):
            raise ValueError('Invalid lifecycle metadata')
        if data['state'] is not None:
            _timestamp(data['transitioned_at'])
            if not self.store._transition_valid({
                    'knowledge_key': key, 'lifecycle_present': key,
                    'lifecycle_reason': data['reason'], 'lifecycle_state': data['state'],
                    'lifecycle_transitioned_at': data['transitioned_at']}):
                raise ValueError('Invalid lifecycle transition provenance')
        return data

    @staticmethod
    def _capacity(conn, key: str) -> None:
        count = conn.execute('SELECT COUNT(*) FROM knowledge_lifecycle_events WHERE knowledge_key=?',
                             (key,)).fetchone()[0]
        if count >= MAX_EVENTS_PER_KEY:
            raise ValueError('Lifecycle history is full; historical evidence is preserved')

    def _event(self, conn, key: str, successor: str | None, kind: str,
               previous: str, current: str, reason: str, refs: list[dict]) -> dict:
        self._capacity(conn, key)
        event = {'event_id': 'kev_' + uuid4().hex, 'knowledge_key': key,
                 'successor_key': successor, 'kind': kind, 'reason': reason,
                 'previous_state': previous, 'current_state': current,
                 'evidence_refs': refs, 'transitioned_at': utc_now()}
        conn.execute('''INSERT INTO knowledge_lifecycle_events(event_id,knowledge_key,
            successor_key,kind,reason,previous_state,current_state,evidence_refs_json,
            transitioned_at) VALUES(?,?,?,?,?,?,?,?,?)''',
            (event['event_id'], key, successor, kind, reason, previous, current,
             json.dumps(refs, sort_keys=True), event['transitioned_at']))
        return event

    def set_policy(self, knowledge_key: str, freshness_class: str, *, reason: str) -> dict:
        key = _key(knowledge_key)
        if freshness_class not in FRESHNESS_POLICIES:
            raise ValueError('Unknown freshness class')
        reason = _reason(reason)
        with closing(self.store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = self._current(conn, key)
            if old['freshness_class'] == freshness_class and old['state'] == 'current':
                return {'status': 'already_assigned', 'knowledge_key': key,
                        'freshness_class': freshness_class}
            refs = self._references(conn, (key,))
            event = self._event(conn, key, None, 'policy_assigned', old['state'] or 'legacy',
                                'current', reason, refs)
            conn.execute('''INSERT INTO knowledge_lifecycle(knowledge_key,freshness_class,
                policy_days,state,successor_key,reason,transitioned_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(knowledge_key) DO UPDATE SET
                freshness_class=excluded.freshness_class,policy_days=excluded.policy_days,
                reason=excluded.reason,transitioned_at=excluded.transitioned_at''',
                (key, freshness_class, FRESHNESS_POLICIES[freshness_class], 'current',
                 None, reason, event['transitioned_at']))
            conn.commit()
        return event

    def supersede(self, older_key: str, newer_key: str, *, reason: str) -> dict:
        old_key, new_key = _key(older_key), _key(newer_key)
        if old_key == new_key:
            raise ValueError('Supersession requires distinct knowledge keys')
        reason = _reason(reason)
        with closing(self.store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            old, new = self._current(conn, old_key), self._current(conn, new_key)
            if self.store._freshness({
                    **new, 'lifecycle_present': new_key if new['state'] else None,
                    'lifecycle_class': new['freshness_class'],
                    'lifecycle_policy_days': new['policy_days'],
                    'lifecycle_state': new['state'], 'lifecycle_reason': new['reason'],
                    'lifecycle_transitioned_at': new['transitioned_at']}, None)['freshness'] != 'fresh':
                raise ValueError('Replacement evidence must be current and fresh')
            old_time = datetime.fromisoformat(old['last_verified_at'])
            new_time = datetime.fromisoformat(new['last_verified_at'])
            if (new_time <= old_time or new['claim_hash'] == old['claim_hash']
                    or not (new['confidence'] > old['confidence']
                            or new['host_count'] > old['host_count']
                            or new['evidence_count'] > old['evidence_count'])):
                raise ValueError('Newer stronger distinct evidence required')
            old_terms, new_terms = _topic_terms(old['claim_text']), _topic_terms(new['claim_text'])
            if not old_terms or len(old_terms & new_terms) < max(2, len(old_terms) // 2):
                raise ValueError('Replacement is not about the same subject')
            refs = self._references(conn, (old_key, new_key))
            event = self._event(conn, old_key, new_key, 'superseded',
                                old['state'] or 'legacy', 'superseded', reason, refs)
            category = old['freshness_class'] or 'stable_engineering'
            conn.execute('''INSERT INTO knowledge_lifecycle(knowledge_key,freshness_class,
                policy_days,state,successor_key,reason,transitioned_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(knowledge_key) DO UPDATE SET state=excluded.state,
                successor_key=excluded.successor_key,reason=excluded.reason,
                transitioned_at=excluded.transitioned_at''',
                (old_key, category, FRESHNESS_POLICIES[category], 'superseded',
                 new_key, reason, event['transitioned_at']))
            conn.commit()
        return event

    def mark_conflict(self, first_key: str, second_key: str, *, reason: str) -> list[dict]:
        first, second = _key(first_key), _key(second_key)
        if first == second:
            raise ValueError('Separate claims required')
        reason = _reason(reason)
        with closing(self.store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            one, two = self._current(conn, first), self._current(conn, second)
            if not _opposition(one['claim_text'], two['claim_text']):
                raise ValueError('Opposing claim text is not deterministically established')
            refs = self._references(conn, (first, second))
            events = []
            for key, current in ((first, one), (second, two)):
                event = self._event(conn, key, None, 'conflicted',
                                    current['state'] or 'legacy', 'conflicted', reason, refs)
                category = current['freshness_class'] or 'stable_engineering'
                conn.execute('''INSERT INTO knowledge_lifecycle(knowledge_key,freshness_class,
                    policy_days,state,successor_key,reason,transitioned_at) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(knowledge_key) DO UPDATE SET state=excluded.state,
                    successor_key=NULL,reason=excluded.reason,transitioned_at=excluded.transitioned_at''',
                    (key, category, FRESHNESS_POLICIES[category], 'conflicted', None,
                     reason, event['transitioned_at']))
                events.append(event)
            conn.commit()
        return events

    def deprecate(self, knowledge_key: str, *, reason: str) -> dict:
        key, reason = _key(knowledge_key), _reason(reason)
        with closing(self.store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = self._current(conn, key)
            refs = self._references(conn, (key,))
            event = self._event(conn, key, None, 'deprecated', old['state'] or 'legacy',
                                'deprecated', reason, refs)
            category = old['freshness_class'] or 'stable_engineering'
            conn.execute('''INSERT INTO knowledge_lifecycle(knowledge_key,freshness_class,
                policy_days,state,successor_key,reason,transitioned_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(knowledge_key) DO UPDATE SET state=excluded.state,
                successor_key=NULL,reason=excluded.reason,transitioned_at=excluded.transitioned_at''',
                (key, category, FRESHNESS_POLICIES[category], 'deprecated', None,
                 reason, event['transitioned_at']))
            conn.commit()
        return event

    def reinforce(self, knowledge_key: str, *, evidence_id: str, source_id: str,
                  source_url: str, confidence: float, verifier_kind: str,
                  evidence_sha256: str, retrieved_at: str, verified: bool,
                  reason: str) -> dict:
        """Atomically append a distinct compatible source and journal consolidation."""
        if verified is not True:
            raise ValueError('Independent verified evidence required')
        key, reason = _key(knowledge_key), _reason(reason)
        evidence_id, evidence_sha256 = _evidence_id(evidence_id), _sha(evidence_sha256)
        source_url, host = _source(source_url)
        retrieved_at, confidence = _timestamp(retrieved_at), _confidence(confidence)
        if confidence < MIN_CONFIDENCE:
            raise ValueError('Evidence confidence below threshold')
        if (not isinstance(source_id, str) or not 1 <= len(source_id) <= 128
                or not isinstance(verifier_kind, str) or not 1 <= len(verifier_kind) <= 160):
            raise ValueError('Invalid source or verifier')
        with closing(self.store._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = self._current(conn, key)
            refs = self._references(conn, (key,))
            if len(refs) >= MAX_REFS_PER_TRANSITION:
                raise ValueError('Evidence reference capacity reached')
            cursor = conn.execute('''INSERT OR IGNORE INTO knowledge_evidence(
                evidence_id,knowledge_key,claim_text,claim_hash,source_id,source_url,
                source_host,confidence,verifier_kind,evidence_sha256,retrieved_at,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (evidence_id,key,old['claim_text'],old['claim_hash'],source_id,source_url,
                 host,confidence,verifier_kind,evidence_sha256,retrieved_at,utc_now()))
            if cursor.rowcount != 1:
                return {'status': 'duplicate_evidence', 'knowledge_key': key}
            summary = conn.execute('''SELECT COUNT(DISTINCT evidence_id),COUNT(DISTINCT source_url),
                COUNT(DISTINCT source_host),MAX(retrieved_at) FROM knowledge_evidence
                WHERE knowledge_key=? AND claim_hash=?''', (key, old['claim_hash'])).fetchone()
            host_rows = conn.execute('''SELECT MAX(confidence) FROM knowledge_evidence
                WHERE knowledge_key=? AND claim_hash=? GROUP BY source_host''',
                (key, old['claim_hash'])).fetchall()
            mean = round(sum(float(row[0]) for row in host_rows) / len(host_rows), 4)
            if mean < MIN_CONFIDENCE:
                raise ValueError('Reinforcement would weaken verified confidence')
            refs = self._references(conn, (key,))
            event = self._event(conn, key, None, 'reinforced', old['state'] or 'legacy',
                                'current', reason, refs)
            conn.execute('''UPDATE consolidated_knowledge SET evidence_count=?,source_count=?,
                host_count=?,confidence=?,last_verified_at=?,consolidated_at=?
                WHERE knowledge_key=?''', (int(summary[0]),int(summary[1]),int(summary[2]),
                 mean,str(summary[3]),event['transitioned_at'],key))
            if old['state'] is not None:
                conn.execute('''UPDATE knowledge_lifecycle SET reason=?,transitioned_at=?
                    WHERE knowledge_key=? AND state='current' ''',
                    (reason,event['transitioned_at'],key))
            conn.commit()
        return {'status': 'reinforced', **event}

    def history(self, knowledge_key: str) -> list[dict]:
        key = _key(knowledge_key)
        with closing(self.store._connect()) as conn:
            rows = conn.execute('''SELECT * FROM knowledge_lifecycle_events
                WHERE knowledge_key=? ORDER BY transitioned_at DESC,event_id DESC LIMIT ?''',
                (key, MAX_EVENTS_PER_KEY + 1)).fetchall()
        if len(rows) > MAX_EVENTS_PER_KEY:
            raise ValueError('Lifecycle history exceeds bound')
        result = []
        for row in rows:
            value = dict(row)
            try:
                refs = json.loads(value.pop('evidence_refs_json'))
                if (not isinstance(refs, list) or not 1 <= len(refs) <= MAX_REFS_PER_TRANSITION
                        or any(not isinstance(ref, dict)
                               or not {'knowledge_key','evidence_id','source_id','source_url',
                                       'source_host','evidence_sha256','claim_hash'} <= set(ref)
                               for ref in refs)):
                    raise ValueError('Invalid lifecycle evidence references')
                value['evidence_refs'] = refs
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError('Invalid lifecycle evidence references') from exc
            result.append(value)
        return result
