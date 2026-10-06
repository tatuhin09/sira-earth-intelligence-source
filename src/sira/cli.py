"""Small CLI. Research commands never execute subprocesses or source .env."""
import argparse
import getpass
import json
from pathlib import Path
import sys

from .benchmark import CASES, benchmark
from .config import Settings, load_key, load_optional_key, save_key
from .controller import research
from .providers.fixture import FixtureProvider
from .providers.tavily import TavilyProvider
from .providers.tavily_extract import TavilyExtract
from .providers.gemini import GeminiModel
from .reading import read_run
from .reading_benchmark import reading_benchmark
from .paper_benchmark import paper_benchmark
from .paper_reading import paper_read_run
from .paper_reading_benchmark import paper_reading_benchmark
from .providers.pdf_text import PdfTextReader
from .papers import papers_run
from .providers.semantic_scholar import SemanticScholarProvider
from .providers.crossref import CrossrefProvider
from .providers.arxiv import ArxivProvider
from .providers.paper_router import EnrichingPaperProvider, FallbackPaperProvider
from .synthesis import answer_run
from .synthesis_benchmark import synthesis_benchmark
from .memory import MemoryStore, learn_all, learn_run
from .language_intelligence import analyze_language_with_store
from .learning_consolidation import LearningConsolidationStore
from .language_semantic_bridge import bridge_multilingual_query
from .memory_benchmark import memory_benchmark
from .improvement import ImprovementStore, plan_improvement, research_memory, run_experiment
from .improvement_benchmark import improvement_benchmark
from .runtime import AutonomousRuntime, RuntimeStateStore, run_autonomous_loop
from .runtime_reliability import run_reliability_check
from .worker_coordination import run_multi_worker_check
from .writer_live_check import run_writer_live_check
from .engineering_canary import run_engineering_canary_check
from .runtime_soak import run_bounded_real_soak
from .runtime_release import run_release_acceptance
from .opportunity import discover_opportunities
from .opportunity_evidence import build_opportunity_evidence
from .opportunity_research import research_opportunity_free
from .opportunity_handoff import run_opportunity_writer_handoff
from .access_requests import AccessRequestStore
from .owner_notifications import OwnerNotificationStore, pump_owner_notifications
from .learning_goals import LearningGoalStore
from .learning_goal_skill_candidates import advisory_skill_candidates
from .learning_goal_practice import execute_practice_candidate
from .access_provisioning import (
    reconcile_approved_access_requests,
    verify_and_satisfy_access_request,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SIRA 1.2: resource-aware autonomous self-improvement runtime")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("research", help="One search; evidence is not fact-checked")
    search.add_argument("question")
    search.add_argument("--provider", choices=("tavily", "fixture"), default="tavily")
    search.add_argument("--max-results", type=int, choices=range(1, 6), default=3)
    search.add_argument("--no-cache", action="store_true")
    bench = commands.add_parser("benchmark", help="Run offline synthetic regression cases")
    suites = bench.add_mutually_exclusive_group()
    suites.add_argument("--reading", action="store_true", help="Run document/evidence fixtures")
    suites.add_argument("--synthesis", action="store_true", help="Run answer/gating fixtures")
    suites.add_argument("--papers", action="store_true", help="Run scholarly-paper fixtures")
    suites.add_argument("--paper-reading", action="store_true", help="Run paper evidence fixtures")
    suites.add_argument("--memory", action="store_true", help="Run local learning-memory fixtures")
    suites.add_argument("--improvement", action="store_true", help="Run isolated improvement-loop fixtures")
    reading = commands.add_parser("read", help="Read sources from an existing search run")
    reading.add_argument("run_id")
    reading.add_argument("--no-cache", action="store_true")
    paper_read = commands.add_parser("paper-read", help="Convert a paper-search run into evidence")
    paper_read.add_argument("paper_run_id")
    paper_read.add_argument("--no-cache", action="store_true")
    papers = commands.add_parser("papers", help="Search up to three scholarly papers with free provider fallback")
    papers.add_argument("question")
    papers.add_argument("--provider", choices=("auto", "semantic-scholar", "crossref", "arxiv"), default="auto")
    papers.add_argument("--no-cache", action="store_true")
    answer = commands.add_parser("answer", help="Synthesize only locally verified evidence claims")
    answer.add_argument("evidence_run_id")
    answer.add_argument("--no-cache", action="store_true")
    learn = commands.add_parser("learn", help="Ingest one run or all compatible runs into local learning memory")
    learn_target = learn.add_mutually_exclusive_group(required=True)
    learn_target.add_argument("run_id", nargs="?")
    learn_target.add_argument("--all", action="store_true", dest="learn_all")
    memory = commands.add_parser("memory", help="Inspect local structured learning memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_search = memory_commands.add_parser("search", help="Search memory summaries and classifications")
    memory_search.add_argument("query")
    memory_search.add_argument("--limit", type=int, default=20)
    memory_show = memory_commands.add_parser("show", help="Show one memory with provenance history")
    memory_show.add_argument("memory_id")
    memory_commands.add_parser("stats", help="Show local memory counts and paths")
    language = commands.add_parser("language", help="Inspect local multilingual input profiles")
    language_commands = language.add_subparsers(dest="language_command", required=True)
    language_analyze = language_commands.add_parser("analyze", help="Profile one text locally")
    language_analyze.add_argument("text")
    language_bridge = language_commands.add_parser("bridge", help="Build a verified multilingual research query")
    language_bridge.add_argument("text")
    language_bridge.add_argument("--allow-model", action="store_true")
    language_bridge.add_argument("--no-cache", action="store_true")
    learning = commands.add_parser("learning", help="Inspect consolidated language mappings and skills")
    learning_commands = learning.add_subparsers(dest="learning_command", required=True)
    learning_commands.add_parser("lexicon", help="Show consolidated language mappings")
    learning_commands.add_parser("skills", help="Show active reusable skills")
    learning_commands.add_parser("stats", help="Show consolidation counts")
    goals = commands.add_parser("goals", help="Manage owner-directed local learning goals")
    goal_commands = goals.add_subparsers(dest="goal_command", required=True)
    add_goal = goal_commands.add_parser("add", help="Save a topic for future autonomous learning")
    add_goal.add_argument("topic")
    add_goal.add_argument("--priority", choices=("normal", "high"), default="normal")
    add_goal.add_argument("--public-research", action="store_true", help="Allow this topic to be sent to free public research services after runtime integration")
    goal_commands.add_parser("list", help="List saved learning goals")
    practice_list = goal_commands.add_parser("practice-list", help="List locally executable provenance exercises for verified goal candidates")
    practice_list.add_argument("goal_id")
    practice = goal_commands.add_parser("practice", help="Run one bounded isolated provenance exercise; does not activate a skill")
    practice.add_argument("goal_id")
    practice.add_argument("candidate_id")
    for action in ("show", "pause", "resume"):
        goal_action = goal_commands.add_parser(action, help=f"{action} one learning goal")
        goal_action.add_argument("goal_id")
    goal_priority = goal_commands.add_parser("set-priority", help="Change a saved goal's priority")
    goal_priority.add_argument("goal_id")
    goal_priority.add_argument("priority", choices=("normal", "high"))
    goal_plan = goal_commands.add_parser("set-plan", help="Add a bounded rotating study plan")
    goal_plan.add_argument("goal_id")
    goal_plan.add_argument("steps", nargs="+", help="Two to twelve research subtopics")
    for action in ("allow-public", "local-only"):
        goal_privacy = goal_commands.add_parser(action, help=f"Change a saved goal to {action}")
        goal_privacy.add_argument("goal_id")
    improve = commands.add_parser("improve", help="Plan and test bounded self-improvement hypotheses")
    improve_commands = improve.add_subparsers(dest="improve_command", required=True)
    improve_commands.add_parser("plan", help="Select the highest-priority unresolved failure memory")
    improve_research = improve_commands.add_parser("research", help="Create or reuse a bounded hypothesis")
    improve_research.add_argument("memory_id")
    improve_experiment = improve_commands.add_parser("experiment", help="Run an isolated offline regression experiment")
    improve_experiment.add_argument("hypothesis_id")
    opportunities = improve_commands.add_parser("opportunities", help="Discover ranked local improvement opportunities")
    opportunities.add_argument("--limit", type=int, default=5)
    evidence = improve_commands.add_parser("evidence", help="Build local evidence for one discovered opportunity")
    evidence.add_argument("opportunity_id")
    free_research = improve_commands.add_parser("free-research", help="Enrich research-ready opportunity evidence with free/public scholarly sources")
    free_research.add_argument("evidence_id")
    writer_handoff = improve_commands.add_parser("writer-handoff", help="Generate, verify, and gate one research-ready opportunity candidate")
    writer_handoff.add_argument("research_id")
    improve_commands.add_parser("status", help="Show persisted improvement artifacts")
    self_command = commands.add_parser("self", help="Inspect or control the autonomous self-improvement runtime")
    self_commands = self_command.add_subparsers(dest="self_command", required=True)
    self_commands.add_parser("status", help="Show persistent autonomous runtime state")
    self_commands.add_parser("on", help="Start the persistent autonomous improvement loop")
    self_commands.add_parser("off", help="Request a graceful autonomous runtime stop")
    self_commands.add_parser("writer-check", help="Run one isolated live code-writer diagnostic without promotion")
    self_commands.add_parser("reliability-check", help="Run an offline crash-recovery and resource-pacing diagnostic")
    self_commands.add_parser("workers-check", help="Run an offline bounded multi-worker coordination diagnostic")
    self_commands.add_parser("canary-check", help="Run the controlled offline engineering canary and bounded-soak readiness check")
    soak_run = self_commands.add_parser("soak-run", help="Run a bounded foreground soak using the real production autonomous cycle")
    soak_run.add_argument("--cycles", type=int, default=2)
    soak_run.add_argument("--max-seconds", type=int, default=900)
    self_commands.add_parser("release-check", help="Run the final offline operations and release-acceptance gate")
    hidden_worker = commands.add_parser("_self-worker", help=argparse.SUPPRESS)
    hidden_worker.add_argument("--generation", type=int, required=True)
    commands.add_parser("setup-key", help="Save a hidden-prompt key to a NEW .env file")
    commands.add_parser("setup-model-key", help="Append a hidden Gemini key without replacing Tavily")
    commands.add_parser("setup-paper-key", help="Append an optional hidden Semantic Scholar API key")
    access = commands.add_parser("access", help="Inspect and resolve owner access requests")
    access_commands = access.add_subparsers(dest="access_command", required=True)
    access_commands.add_parser("list", help="List access requests")
    access_commands.add_parser("resume-ready", help="List satisfied parked tasks ready for reselection")
    access_commands.add_parser("reconcile", help="Verify approved credential requests against local provisioning")
    access_show = access_commands.add_parser("show", help="Show one access request")
    access_show.add_argument("request_id")
    for action in ("approve", "deny", "cancel", "satisfy", "consume-resume"):
        command = access_commands.add_parser(action, help=f"{action} one access request")
        command.add_argument("request_id")
    notifications = commands.add_parser("notifications", help="Inspect and deliver owner notifications")
    notification_commands = notifications.add_subparsers(dest="notification_command", required=True)
    notification_commands.add_parser("list", help="List durable owner notifications")
    notification_commands.add_parser("deliver", help="Deliver eligible pending desktop notifications")
    notification_ack = notification_commands.add_parser("ack", help="Acknowledge one delivered notification")
    notification_ack.add_argument("notification_id")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command in ("setup-key", "setup-model-key", "setup-paper-key"):
            names = {"setup-key": "TAVILY_API_KEY", "setup-model-key": "GEMINI_API_KEY",
                     "setup-paper-key": "SIRA_SEMANTIC_SCHOLAR_API_KEY"}
            labels = {"setup-key": "Tavily", "setup-model-key": "Gemini",
                      "setup-paper-key": "Semantic Scholar"}
            name = names[args.command]
            label = labels[args.command]
            path = save_key(root, name, getpass.getpass(f"{label} API key (hidden): ").strip())
            reconciliation = reconcile_approved_access_requests(root)
            print(json.dumps({"status": "key_saved", "name": name, "file": str(path),
                              "access_reconciliation": reconciliation}))
            return 0
        if args.command == "access":
            store = AccessRequestStore(root)
            if args.access_command == "list":
                result = store.snapshot()
            elif args.access_command == "resume-ready":
                result = {
                    "schema": "sira.access_resume_ready.v1",
                    "requests": list(store.resume_ready()),
                }
            elif args.access_command == "reconcile":
                result = reconcile_approved_access_requests(root)
            elif args.access_command == "show":
                result = store.load(args.request_id)
            elif args.access_command == "approve":
                result = store.approve(args.request_id)
            elif args.access_command == "deny":
                result = store.deny(args.request_id)
            elif args.access_command == "cancel":
                result = store.cancel(args.request_id)
            elif args.access_command == "satisfy":
                result = verify_and_satisfy_access_request(root, args.request_id)
            elif args.access_command == "consume-resume":
                result = store.consume_resume(args.request_id)
            else:
                raise AssertionError("unreachable access command")
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "notifications":
            store = OwnerNotificationStore(root)
            if args.notification_command == "list":
                store.reconcile_access_requests()
                result = store.snapshot()
            elif args.notification_command == "deliver":
                result = pump_owner_notifications(root)
            elif args.notification_command == "ack":
                result = store.acknowledge(args.notification_id)
            else:
                raise AssertionError("unreachable notification command")
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "language":
            if args.language_command == "analyze":
                result = analyze_language_with_store(root, args.text).to_dict()
            elif args.language_command == "bridge":
                result = bridge_multilingual_query(
                    root,
                    args.text,
                    allow_model=args.allow_model,
                    use_cache=not args.no_cache,
                )
            else:
                raise AssertionError("unreachable language command")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "goals":
            store = LearningGoalStore(root)
            if args.goal_command == "add":
                result = store.create(args.topic, priority=args.priority,
                                      public_research_allowed=args.public_research)
            elif args.goal_command == "list":
                rows = store.list()
                result = {"schema": "sira.learning_goals.v1", "goals": rows,
                          "goal_count": len(rows), "api_requests": 0,
                          "model_requests": 0, "paid_spending": False}
            elif args.goal_command == "show":
                result = store.get(args.goal_id)
            elif args.goal_command == "practice-list":
                result = {"schema": "sira.learning_practice_candidates.v1",
                          "goal_id": args.goal_id,
                          "candidates": advisory_skill_candidates(root, store.get(args.goal_id)),
                          "skill_activated": False, "paid_spending": False}
            elif args.goal_command == "practice":
                result = execute_practice_candidate(root, args.goal_id, args.candidate_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["status"] == "completed" else 1
            elif args.goal_command in {"pause", "resume"}:
                result = store.set_status(args.goal_id,
                                          "paused" if args.goal_command == "pause" else "active")
            elif args.goal_command == "set-priority":
                result = store.set_policy(args.goal_id, priority=args.priority)
            elif args.goal_command == "set-plan":
                result = store.set_plan(args.goal_id, args.steps)
            else:
                result = store.set_policy(args.goal_id,
                                          public_research_allowed=args.goal_command == "allow-public")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "learning":
            store = LearningConsolidationStore(root)
            if args.learning_command == "lexicon":
                result = {"schema":"sira.consolidated_language_lexicon.v1","lexicon":store.active_lexicon(),"authority_granted":False,"promotion_authorized":False}
            elif args.learning_command == "skills":
                result = {"schema":"sira.consolidated_skills.v1","skills":store.list_skills(),"authority_granted":False,"promotion_authorized":False}
            else:
                result = store.stats()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "benchmark":
            path, report = (improvement_benchmark(root) if args.improvement else
                            memory_benchmark(root) if args.memory else
                            paper_reading_benchmark(root) if args.paper_reading else
                            paper_benchmark(root) if args.papers else
                            synthesis_benchmark(root) if args.synthesis else
                            reading_benchmark(root) if args.reading else benchmark(root))
            print(json.dumps({"status": "passed" if report["failed"] == 0 else "failed",
                              "passed": report["passed"], "failed": report["failed"],
                              "api_requests": report["api_requests"], "report": str(path)}))
            return int(report["failed"] > 0)
        if args.command == "_self-worker":
            result = run_autonomous_loop(root, args.generation)
            print(json.dumps(result, indent=2))
            return 0 if result.get("status") in {"stopped", "stale_generation", "ownership_not_acquired"} else 1
        if args.command == "self":
            if args.self_command == "writer-check":
                result = run_writer_live_check(root)
                print(json.dumps(result, indent=2))
                return 0 if result.get("status") == "passed" else 1
            if args.self_command == "reliability-check":
                result = run_reliability_check(root)
                print(json.dumps(result, indent=2))
                return 0 if result.get("status") == "passed" else 1
            if args.self_command == "workers-check":
                result = run_multi_worker_check(root)
                print(json.dumps(result, indent=2))
                return 0 if result.get("status") == "passed" else 1
            if args.self_command == "canary-check":
                result = run_engineering_canary_check(root)
                print(json.dumps(result, indent=2))
                return 0 if result.get("status") == "passed" else 1
            if args.self_command == "soak-run":
                result = run_bounded_real_soak(
                    root,
                    max_cycles=args.cycles,
                    max_seconds=args.max_seconds,
                )
                print(json.dumps(result, indent=2))
                return 0 if result.get("status") == "passed" else 1
            if args.self_command == "release-check":
                result = run_release_acceptance(root)
                print(json.dumps(result, indent=2))
                return 0 if result.get("release_ready") is True else 1
            runtime = AutonomousRuntime(root)
            result = runtime.start() if args.self_command == "on" else (runtime.stop() if args.self_command == "off" else RuntimeStateStore(root).status())
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "improve":
            if args.improve_command == "plan":
                print(json.dumps(plan_improvement(root), indent=2))
            elif args.improve_command == "research":
                print(json.dumps(research_memory(root, args.memory_id), indent=2))
            elif args.improve_command == "experiment":
                print(json.dumps(run_experiment(root, args.hypothesis_id), indent=2))
            elif args.improve_command == "opportunities":
                print(json.dumps(discover_opportunities(root, limit=args.limit), indent=2))
            elif args.improve_command == "evidence":
                print(json.dumps(build_opportunity_evidence(root, args.opportunity_id), indent=2))
            elif args.improve_command == "free-research":
                print(json.dumps(research_opportunity_free(root, args.evidence_id), indent=2))
            elif args.improve_command == "writer-handoff":
                print(json.dumps(run_opportunity_writer_handoff(root, args.research_id), indent=2))
            else:
                print(json.dumps(ImprovementStore(root).status(), indent=2))
            return 0
        if args.command == "learn":
            result = learn_all(root) if args.learn_all else learn_run(root, args.run_id)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "memory":
            store = MemoryStore(root)
            if args.memory_command == "search":
                print(json.dumps({"query": args.query, "results": store.search(args.query, args.limit)}, indent=2))
            elif args.memory_command == "show":
                print(json.dumps(store.show(args.memory_id), indent=2))
            else:
                print(json.dumps(store.stats(), indent=2))
            return 0
        if args.command == "paper-read":
            settings = Settings(root, max_results=3)
            path = paper_read_run(root, args.paper_run_id, PdfTextReader(timeout=settings.timeout_seconds),
                                  use_cache=not args.no_cache)
            data = json.loads((path / "evidence.json").read_text(encoding="utf-8"))
            print(json.dumps({"run_id": data["run_id"], "parent_run_id": data["parent_run_id"],
                              "status": data["status"], "metrics": data["metrics"], "error": data["error"],
                              "source_statuses": [{"source_id": d["source_id"], "status": d["status"],
                                                   "origin": d.get("content_origin")} for d in data["documents"]],
                              "report": str(path / "report.html"),
                              "evidence": str(path / "evidence.json")}, indent=2))
            return 130 if data["status"] == "cancelled" else int(data["status"] not in ("completed", "partial"))
        if args.command == "papers":
            settings = Settings(root, max_results=3)
            semantic = SemanticScholarProvider(
                api_key=load_optional_key(root, "SIRA_SEMANTIC_SCHOLAR_API_KEY"),
                timeout=settings.timeout_seconds,
                max_attempts=1 if args.provider == "auto" else 3,
            )
            crossref = CrossrefProvider(timeout=settings.timeout_seconds)
            arxiv = ArxivProvider(timeout=settings.timeout_seconds)
            if args.provider == "auto":
                provider = EnrichingPaperProvider(FallbackPaperProvider((semantic, crossref)), arxiv)
            elif args.provider == "semantic-scholar":
                provider = semantic
            elif args.provider == "crossref":
                provider = crossref
            else:
                provider = arxiv
            path = papers_run(root, args.question, provider, settings, use_cache=not args.no_cache)
            data = json.loads((path / "papers.json").read_text(encoding="utf-8"))
            print(json.dumps({"run_id": data["run_id"], "status": data["status"],
                              "papers": len(data["papers"]), "metrics": data["metrics"],
                              "error": data["error"], "result": str(path / "papers.json")}, indent=2))
            return 130 if data["status"] == "cancelled" else int(data["status"] not in ("completed", "no_results"))
        if args.command == "read":
            path = read_run(root, args.run_id, TavilyExtract(load_key(root)), use_cache=not args.no_cache)
            data = json.loads((path / "evidence.json").read_text(encoding="utf-8"))
            print(json.dumps({"run_id": data["run_id"], "parent_run_id": data["parent_run_id"],
                              "status": data["status"], "metrics": data["metrics"], "error": data["error"],
                              "source_statuses": [{"source_id": d["source_id"], "status": d["status"]}
                                                  for d in data["documents"]],
                              "report": str(path / "report.html"),
                              "evidence": str(path / "evidence.json")}, indent=2))
            return 130 if data["status"] == "cancelled" else int(data["status"] not in ("completed", "partial"))
        if args.command == "answer":
            model = GeminiModel(load_key(root, "GEMINI_API_KEY"))
            path = answer_run(root, args.evidence_run_id, model, use_cache=not args.no_cache)
            data = json.loads((path / "answer.json").read_text(encoding="utf-8"))
            print(json.dumps({"run_id": data["run_id"], "evidence_run_id": data["evidence_run_id"],
                              "status": data["status"], "accepted_claims": len(data["accepted_claims"]),
                              "rejected_claims": len(data["rejected_claims"]),
                              "metrics": data["metrics"], "error": data["error"],
                              "answer": str(path / "answer.md"),
                              "report": str(path / "answer.html")}, indent=2))
            return int(data["status"] == "failed")
        settings = Settings(root, max_results=args.max_results)
        provider = (FixtureProvider(CASES) if args.provider == "fixture"
                    else TavilyProvider(load_key(root), timeout=settings.timeout_seconds))
        path = research(args.question, provider, settings, use_cache=not args.no_cache)
        result = json.loads((path / "result.json").read_text(encoding="utf-8"))
        print(json.dumps({"run_id": result["run_id"], "status": result["status"],
                          "sources": len(result["sources"]), "metrics": result["metrics"],
                          "error": result["error"], "result": str(path / "result.json")}, indent=2))
        return 130 if result["status"] == "cancelled" else int(result["status"] != "completed")
    except ValueError as error:
        # ValueError messages originate in our config/query validation, never HTTP bodies.
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("Storage error: check directory permissions and free disk space.", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
