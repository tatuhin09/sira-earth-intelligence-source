"""Owner-directed learning goals, stored locally without research side effects."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Iterator
from uuid import uuid4

from .models import utc_now
from .storage import write_json
from .learning_goal_progress import next_study_step, study_step_statistics
from .learning_goal_followup import pending_question, search_query_for_focus, valid_hint


MAX_GOALS = 1000
MAX_STUDY_STEPS = 12
MAX_STUDY_HISTORY = 32
GOAL_RETRY_SECONDS = 24 * 60 * 60
GOAL_DISTINCT_STEP_SECONDS = 30 * 60
RESEARCH_POLICY_VERSION = 2
_INCOMPLETE_RESEARCH_OUTCOMES = frozenset({
    "sources_discovered_needs_verification",
    "independent_source_unavailable",
    "research_sources_insufficient",
    "research_failed",
})
_GOAL_ID = re.compile(r"lg_[0-9a-f]{32}\Z")
_PRIORITIES = {"high": 0, "normal": 1}


def _valid_study_plan(value: object) -> bool:
    return (isinstance(value, list) and 2 <= len(value) <= MAX_STUDY_STEPS
            and all(isinstance(step, str) and 1 <= len(step) <= 200
                    and step == " ".join(step.split()) for step in value)
            and len({step.casefold() for step in value}) == len(value))


def _valid_study_state(row: dict) -> bool:
    if "study_plan" not in row:
        return ("study_next_index" not in row and "study_history" not in row
                and "study_step_stats" not in row)
    plan = row["study_plan"]
    if not _valid_study_plan(plan):
        return False
    index = row.get("study_next_index")
    history = row.get("study_history")
    if (type(index) is not int or not 0 <= index < len(plan)
            or not isinstance(history, list) or len(history) > MAX_STUDY_HISTORY):
        return False
    history_valid = all(isinstance(entry, dict)
               and type(entry.get("index")) is int
               and 0 <= entry["index"] < len(plan)
               and entry.get("query") == plan[entry["index"]]
               and isinstance(entry.get("outcome"), str)
               and 1 <= len(entry["outcome"]) <= 120
               and type(entry.get("attempted_at_epoch")) in (int, float)
               and math.isfinite(entry["attempted_at_epoch"])
               and entry["attempted_at_epoch"] >= 0
               and type(entry.get("verified_claim_count")) is int
               and 0 <= entry["verified_claim_count"] <= 100
               for entry in history)
    if not history_valid:
        return False
    stats = row.get("study_step_stats")
    if stats is None:
        return True  # Older stored plans gain durable counters on their next attempt.
    return (isinstance(stats, list) and len(stats) == len(plan)
            and all(isinstance(item, dict)
                    and type(item.get("attempt_count")) is int
                    and 0 <= item["attempt_count"] <= 1_000_000_000
                    and type(item.get("verified_claim_events")) is int
                    and 0 <= item["verified_claim_events"] <= 1_000_000_000
                    and (item.get("last_attempt_at_epoch") is None or
                         (type(item["last_attempt_at_epoch"]) in (int, float)
                          and math.isfinite(item["last_attempt_at_epoch"])
                          and item["last_attempt_at_epoch"] >= 0))
                    and (item.get("last_outcome") is None or
                         (isinstance(item["last_outcome"], str)
                          and 1 <= len(item["last_outcome"]) <= 120))
                    and (item["attempt_count"] > 0 or
                         (item["verified_claim_events"] == 0
                          and item.get("last_attempt_at_epoch") is None
                          and item.get("last_outcome") is None))
                    for item in stats))


def _valid_followups(row: dict) -> bool:
    followups = row.get("research_followups")
    if followups is None:
        return "research_followups" not in row
    allowed = {row["topic"], *(row.get("study_plan") or [])}
    if (not isinstance(followups, dict) or len(followups) > MAX_STUDY_STEPS + 1
            or not set(followups) <= allowed):
        return False
    for focus, item in followups.items():
        if (not isinstance(item, dict) or not {"attempt_count", "last_outcome"} <= set(item)
                or not set(item) <= {"attempt_count", "last_outcome", "hint", "questions"}
                or type(item.get("attempt_count")) is not int
                or not 1 <= item["attempt_count"] <= 1_000_000_000
                or not isinstance(item.get("last_outcome"), str)
                or not 1 <= len(item["last_outcome"]) <= 120
                or ("hint" in item and not valid_hint(focus, item["hint"]))):
            return False
        questions = item.get("questions", [])
        if "questions" in item and (not isinstance(questions, list)
                                    or not 1 <= len(questions) <= 3):
            return False
        if ("questions" in item and (any(
                not isinstance(question, dict)
                or set(question) != {"term", "source_hosts", "source_urls", "attempted"}
                or type(question["attempted"]) is not bool
                or not valid_hint(focus, {key: question[key] for key in
                                          ("term", "source_hosts", "source_urls")})
                for question in questions)
                or len({question["term"] for question in questions}) != len(questions))):
            return False
    return True


def _accelerated_study_due(row: dict, elapsed: float, global_elapsed: float) -> bool:
    """Pace fresh steps and one independently sourced gap query across goals."""
    if (min(elapsed, global_elapsed) < GOAL_DISTINCT_STEP_SECONDS
            or not row["public_research_allowed"]
            or row.get("last_outcome") == "research_started"):
        return False
    history = row.get("study_history") or []
    if not history:
        return False
    last = history[-1]
    next_index = row["study_next_index"]
    if last["attempted_at_epoch"] != row["last_attempt_at_epoch"]:
        return False
    stats = study_step_statistics(row)[next_index]
    if stats["attempt_count"] == 0:
        return last["index"] != next_index
    if stats["verified_claim_events"] != 0:
        return False
    focus = row["study_plan"][next_index]
    followup = (row.get("research_followups") or {}).get(focus) or {}
    return (pending_question(focus, followup) is not None
            and search_query_for_focus(row, focus)[1] == "unverified_title_followup")


class LearningGoalStore:
    def __init__(self, root: Path):
        self.directory = Path(root).resolve() / "memory"
        self.path = self.directory / "learning_goals.json"
        self.lock_path = self.directory / "learning_goals.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self) -> dict[str, dict]:
        if self.path.is_symlink():
            raise ValueError("Learning goal state must be a regular file")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Learning goal state is corrupt") from exc
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not isinstance(value.get("goals"), dict) or len(value["goals"]) > MAX_GOALS):
            raise ValueError("Learning goal state has an unsupported format")
        for goal_id, row in value["goals"].items():
            if (not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id)
                    or not isinstance(row, dict) or row.get("goal_id") != goal_id
                    or not isinstance(row.get("topic"), str)
                    or not 1 <= len(row["topic"]) <= 500
                    or not isinstance(row.get("priority"), str)
                    or row["priority"] not in _PRIORITIES
                    or not isinstance(row.get("status"), str)
                    or row["status"] not in {"active", "paused"}
                    or not isinstance(row.get("created_at"), str)
                    or not isinstance(row.get("updated_at"), str)
                    or ("last_attempt_at_epoch" in row and (
                        type(row["last_attempt_at_epoch"]) not in (int, float)
                        or not math.isfinite(row["last_attempt_at_epoch"])
                        or row["last_attempt_at_epoch"] < 0))
                    or ("last_outcome" in row and (
                        not isinstance(row["last_outcome"], str)
                        or len(row["last_outcome"]) > 120))
                    or ("last_research_policy_version" in row and (
                        type(row["last_research_policy_version"]) is not int
                        or not 1 <= row["last_research_policy_version"] <= 1_000_000))
                    or type(row.get("public_research_allowed")) is not bool
                    or not _valid_study_state(row)
                    or not _valid_followups(row)):
                raise ValueError("Learning goal state contains an invalid goal")
        return value["goals"]

    def _save(self, goals: dict[str, dict]) -> None:
        write_json(self.path, {
            "schema_version": 1,
            "updated_at": utc_now(),
            "goals": goals,
        })

    def create(self, topic: str, *, priority: str = "normal",
               public_research_allowed: bool = False) -> dict:
        if not isinstance(topic, str):
            raise ValueError("Topic must be text")
        topic = " ".join(topic.split())
        if not 1 <= len(topic) <= 500:
            raise ValueError("Topic must contain 1..500 characters")
        if not isinstance(priority, str) or priority not in _PRIORITIES:
            raise ValueError("Priority must be high or normal")
        if type(public_research_allowed) is not bool:
            raise ValueError("Public research permission must be a boolean")
        with self._locked():
            goals = self._load()
            for row in goals.values():
                if row["topic"].casefold() == topic.casefold():
                    if row["public_research_allowed"] != public_research_allowed or row["priority"] != priority:
                        raise ValueError("Existing topic has different priority or public research permission")
                    return dict(row)
            if len(goals) >= MAX_GOALS:
                raise ValueError("Learning goal storage is full")
            now = utc_now()
            goal_id = "lg_" + uuid4().hex
            row = {
                "goal_id": goal_id,
                "topic": topic,
                "priority": priority,
                "status": "active",
                "public_research_allowed": public_research_allowed,
                "created_at": now,
                "updated_at": now,
            }
            goals[goal_id] = row
            self._save(goals)
            return dict(row)

    def list(self) -> list[dict]:
        return sorted((dict(row) for row in self._load().values()),
                      key=lambda row: (_PRIORITIES[row["priority"]], row["created_at"], row["goal_id"]))

    def get(self, goal_id: str) -> dict:
        if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
            raise ValueError("Invalid learning goal id")
        row = self._load().get(goal_id)
        if row is None:
            raise ValueError("Learning goal not found")
        return dict(row)

    def set_status(self, goal_id: str, status: str) -> dict:
        if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
            raise ValueError("Invalid learning goal id")
        if not isinstance(status, str) or status not in {"active", "paused"}:
            raise ValueError("Invalid learning goal status")
        with self._locked():
            goals = self._load()
            row = goals.get(goal_id)
            if row is None:
                raise ValueError("Learning goal not found")
            if row["status"] != status:
                row["status"] = status
                row["updated_at"] = utc_now()
                self._save(goals)
            return dict(row)

    def set_policy(self, goal_id: str, *, priority: str | None = None,
                   public_research_allowed: bool | None = None) -> dict:
        if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
            raise ValueError("Invalid learning goal id")
        if priority is not None and (not isinstance(priority, str) or priority not in _PRIORITIES):
            raise ValueError("Priority must be high or normal")
        if public_research_allowed is not None and type(public_research_allowed) is not bool:
            raise ValueError("Public research permission must be a boolean")
        with self._locked():
            goals = self._load()
            row = goals.get(goal_id)
            if row is None:
                raise ValueError("Learning goal not found")
            new_priority = priority if priority is not None else row["priority"]
            new_public = (public_research_allowed if public_research_allowed is not None
                          else row["public_research_allowed"])
            if (new_priority, new_public) != (row["priority"], row["public_research_allowed"]):
                row["priority"] = new_priority
                row["public_research_allowed"] = new_public
                row["updated_at"] = utc_now()
                self._save(goals)
            return dict(row)

    def set_plan(self, goal_id: str, steps: list[str]) -> dict:
        """Add an immutable bounded study program without resetting retry time."""
        if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
            raise ValueError("Invalid learning goal id")
        if not isinstance(steps, (list, tuple)):
            raise ValueError("Study plan must contain 2..12 distinct subtopics")
        normalized = [" ".join(step.split()) if isinstance(step, str) else step
                      for step in steps]
        if not _valid_study_plan(normalized):
            raise ValueError("Study plan must contain 2..12 distinct subtopics")
        with self._locked():
            goals = self._load()
            row = goals.get(goal_id)
            if row is None:
                raise ValueError("Learning goal not found")
            if "study_plan" in row:
                if row["study_plan"] != normalized:
                    raise ValueError("Existing study plan cannot be silently replaced")
                return dict(row)
            row["study_plan"] = normalized
            row["study_next_index"] = 0
            row["study_history"] = []
            row["study_step_stats"] = study_step_statistics(row)
            row["updated_at"] = utc_now()
            self._save(goals)
            return dict(row)

    def candidates(self, *, now_epoch: float | None = None, limit: int = 10) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Goal candidate limit must be 1..20")
        when = time.time() if now_epoch is None else now_epoch
        if type(when) not in (int, float) or not math.isfinite(when) or when < 0:
            raise ValueError("now_epoch must be non-negative and finite")
        rows = self.list()
        most_recent_public = max((row["last_attempt_at_epoch"] for row in rows
                                  if row["public_research_allowed"]
                                  and "last_attempt_at_epoch" in row), default=float("-inf"))
        eligible = [row for row in rows
                    if row["status"] == "active" and (
                        "last_attempt_at_epoch" not in row
                        or float(when) - row["last_attempt_at_epoch"] >= GOAL_RETRY_SECONDS
                        or _accelerated_study_due(
                            row, float(when) - row["last_attempt_at_epoch"],
                            float(when) - most_recent_public)
                        or (row["public_research_allowed"]
                            and row.get("last_outcome") in _INCOMPLETE_RESEARCH_OUTCOMES
                            and row.get("last_research_policy_version", 0) < RESEARCH_POLICY_VERSION))]
        eligible.sort(key=lambda row: (_PRIORITIES[row["priority"]],
                                       row.get("last_attempt_at_epoch", -1.0),
                                       row["created_at"], row["goal_id"]))
        return eligible[:limit]

    def choose_study_step(self, goal_id: str, *, expected_index: int,
                          selected_index: int) -> dict:
        """Atomically move to an existing owner step after an adaptive decision."""
        with self._locked():
            goals = self._load()
            row = goals.get(goal_id)
            if (row is None or row.get("status") != "active"
                    or type(expected_index) is not int
                    or row.get("study_next_index") != expected_index
                    or type(selected_index) is not int
                    or not 0 <= selected_index < len(row.get("study_plan") or [])):
                raise ValueError("Adaptive study selection is stale")
            if selected_index != expected_index:
                row["study_next_index"] = selected_index
                row["updated_at"] = utc_now()
                self._save(goals)
            return dict(row)

    def record_attempt(self, goal_id: str, outcome: str, *, at_epoch: float | None = None,
                       artifact: str | None = None,
                       research_policy_version: int | None = None,
                       study_step_index: int | None = None,
                       verified_claim_count: int = 0,
                       research_focus: str | None = None,
                       followup_hint: dict | None = None,
                       followup_questions: list[dict] | None = None,
                       attempted_question_term: str | None = None) -> dict:
        if not isinstance(goal_id, str) or not _GOAL_ID.fullmatch(goal_id):
            raise ValueError("Invalid learning goal id")
        if not isinstance(outcome, str) or not 1 <= len(outcome) <= 120:
            raise ValueError("Invalid learning goal outcome")
        when = time.time() if at_epoch is None else at_epoch
        if type(when) not in (int, float) or not math.isfinite(when) or when < 0:
            raise ValueError("at_epoch must be non-negative and finite")
        if artifact is not None and (not isinstance(artifact, str) or len(artifact) > 1024):
            raise ValueError("Invalid learning goal artifact")
        if research_policy_version is not None and (
                type(research_policy_version) is not int
                or not 1 <= research_policy_version <= RESEARCH_POLICY_VERSION):
            raise ValueError("Invalid learning goal research policy version")
        if (study_step_index is not None and type(study_step_index) is not int
                or type(verified_claim_count) is not int
                or not 0 <= verified_claim_count <= 100):
            raise ValueError("Invalid study attempt")
        if (research_focus is not None and not isinstance(research_focus, str)
                or followup_hint is not None and (
                    research_focus is None or not valid_hint(research_focus, followup_hint))
                or followup_questions is not None and (
                    research_focus is None or not isinstance(followup_questions, list)
                    or len(followup_questions) > 3 or any(
                        not valid_hint(research_focus, question)
                        for question in followup_questions))
                or attempted_question_term is not None and (
                    research_focus is None or not isinstance(attempted_question_term, str)
                    or not 3 <= len(attempted_question_term) <= 24)):
            raise ValueError("Invalid research follow-up")
        with self._locked():
            goals = self._load()
            row = goals.get(goal_id)
            if row is None:
                raise ValueError("Learning goal not found")
            if study_step_index is not None:
                if ("study_plan" not in row
                        or row["study_next_index"] != study_step_index):
                    raise ValueError("Learning study step is stale")
                stats = study_step_statistics(row)
                current_step = stats[study_step_index]
                current_step["attempt_count"] += 1
                current_step["verified_claim_events"] += verified_claim_count
                current_step["last_attempt_at_epoch"] = float(when)
                current_step["last_outcome"] = outcome
                row["study_history"] = (row["study_history"] + [{
                    "index": study_step_index,
                    "query": row["study_plan"][study_step_index],
                    "outcome": outcome,
                    "attempted_at_epoch": float(when),
                    "verified_claim_count": verified_claim_count,
                }])[-MAX_STUDY_HISTORY:]
                row["study_step_stats"] = stats
                row["study_next_index"] = next_study_step(stats, row["study_history"])
            if research_focus is not None:
                expected = (row["study_plan"][study_step_index]
                            if study_step_index is not None else row["topic"])
                if research_focus != expected:
                    raise ValueError("Research follow-up must match owner focus")
                followups = dict(row.get("research_followups") or {})
                previous = followups.get(research_focus, {})
                entry = {"attempt_count": previous.get("attempt_count", 0) + 1,
                         "last_outcome": outcome}
                if followup_hint is not None:
                    entry["hint"] = followup_hint
                elif "hint" in previous:
                    entry["hint"] = previous["hint"]
                if "questions" in previous or followup_questions:
                    questions = [dict(question) for question in previous.get("questions", [])]
                    terms = {question["term"] for question in questions}
                    for question in followup_questions or []:
                        if question["term"] not in terms and len(questions) < 3:
                            questions.append({**question, "attempted": False})
                            terms.add(question["term"])
                    if attempted_question_term is not None:
                        for question in questions:
                            if question["term"] == attempted_question_term:
                                question["attempted"] = True
                    entry["questions"] = questions
                followups[research_focus] = entry
                row["research_followups"] = followups
                if study_step_index is not None and all(
                        stat["attempt_count"] for stat in row["study_step_stats"]):
                    pending = [index for index, stat in enumerate(row["study_step_stats"])
                               if stat["verified_claim_events"] == 0
                               and pending_question(row["study_plan"][index],
                                   followups.get(row["study_plan"][index])) is not None]
                    if pending:
                        row["study_next_index"] = min(pending, key=lambda index: (
                            row["study_step_stats"][index]["last_attempt_at_epoch"], index))
            row["last_attempt_at_epoch"] = float(when)
            row["last_outcome"] = outcome
            if research_policy_version is not None:
                row["last_research_policy_version"] = max(
                    row.get("last_research_policy_version", 0), research_policy_version
                )
            if artifact is not None:
                row["last_report"] = artifact
            row["updated_at"] = utc_now()
            self._save(goals)
            return dict(row)
