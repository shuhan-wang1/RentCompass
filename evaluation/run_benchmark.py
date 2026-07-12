"""RentCompass offline evaluation runner.

Entry point::

    python -m evaluation.run_benchmark --smoke --offline --out evaluation/results/_smoke_offline

Drives the REAL agent graph (``build_agent_graph``) against the Phase-2 benchmark,
capturing events via ``evaluation.metrics.collector`` and grading each turn with the
deterministic ``evaluation.metrics.graders``. Two modes:

* ``--offline`` — deterministic, UNBILLED. Fake-LLM (``evaluation.metrics.fake_llm``)
  + every tool stubbed/fixture-replayed. Validates MECHANICS ONLY
  (routing / tool selection / latency / memory-isolation), NOT grounding quality
  (the fake responder emits canned text). summary.json is marked accordingly.
* ``--live`` — real DeepSeek via ModelRouter. Cases WITH a ``fixture`` replay the
  recorded tool output deterministically; cases WITHOUT a fixture run tools
  in-process (may hit cache/live network — free but nondeterministic).

Isolation: ALL state (checkpointer, conversation db, listing cache, idempotency,
ChromaDB agent memory) is redirected to per-run temp dirs. The repo's real
``.runtime/`` and ``chroma_db_*`` are never touched.

Cost cap, resume checkpoint, and result writers are all here. Latency uses
``perf_counter`` only.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "evaluation" / "benchmark"
CASES_PATH = BENCH_DIR / "cases.jsonl"
SCHEMA_PATH = BENCH_DIR / "schema.json"
FIXTURES_DIR = BENCH_DIR / "fixtures"


# --------------------------------------------------------------------------- #
# Environment bootstrap  (MUST run before importing app/core modules)
# --------------------------------------------------------------------------- #
def _bootstrap_env(state_dir: Path, events_log: Path) -> None:
    """Put the repo on sys.path and point ALL mutable state at temp dirs."""
    for p in (REPO_ROOT, REPO_ROOT / "app", REPO_ROOT / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))

    state_dir.mkdir(parents=True, exist_ok=True)
    # Activate eval capture + force in-process tools + DeepSeek router shape.
    os.environ["RENTCOMPASS_EVAL"] = "1"
    os.environ["USE_MCP_TOOLS"] = "0"
    os.environ.setdefault("LLM_PROVIDER", "deepseek")
    os.environ["RENTCOMPASS_EVAL_LOG"] = str(events_log)
    # Redirect every state sink AWAY from the repo's real .runtime / chroma dirs.
    os.environ["CHECKPOINT_PATH"] = str(state_dir / "checkpoints.sqlite3")
    os.environ["CONVERSATION_DB_PATH"] = str(state_dir / "conversation.sqlite3")
    os.environ["SEARCH_LISTING_CACHE_PATH"] = str(state_dir / "listing_cache.sqlite3")
    os.environ["IDEMPOTENCY_DB"] = str(state_dir / "idempotency.sqlite3")
    os.environ["AUTH_DB_PATH"] = str(state_dir / "users.json")
    # Load the REAL app/.env so LIVE runs use the genuine DeepSeek key. llm_config
    # also loads it, but only with override=False and AFTER this bootstrap runs — so
    # unless we populate os.environ here first, the offline placeholder below would
    # win and every live call would 401.
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / "app" / ".env", override=False)
    except Exception:
        pass
    # Offline safety net only: if no key is configured anywhere, a dummy keeps
    # ChatOpenAI construction happy (offline mode never actually calls it).
    os.environ.setdefault("DEEPSEEK_API_KEY", "offline-eval-placeholder")


# --------------------------------------------------------------------------- #
# Case loading + selection
# --------------------------------------------------------------------------- #
def load_cases() -> List[dict]:
    rows: List[dict] = []
    with CASES_PATH.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"cases.jsonl line {lineno}: invalid JSON: {exc}")
    return rows


def schema_validate(cases: List[dict]) -> List[str]:
    """Schema-validate every case (reuses jsonschema if available)."""
    try:
        import jsonschema
        from jsonschema import Draft202012Validator
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        problems = []
        for c in cases:
            for e in validator.iter_errors(c):
                problems.append(f"{c.get('case_id','?')}: {'/'.join(str(p) for p in e.path)}: {e.message}")
        return problems
    except Exception:
        return []  # structural fallback: skip (validate.py has the strict path)


def select_cases(cases: List[dict], *, smoke: bool, limit: Optional[int],
                 category: Optional[str]) -> List[dict]:
    out = cases
    if smoke:
        out = [c for c in out if c.get("smoke") is True]
    if category:
        out = [c for c in out if c.get("category") == category or c.get("category", "").startswith(category)]
    if limit is not None:
        out = out[:limit]
    return out


# --------------------------------------------------------------------------- #
# Fixtures -> per-tool evidence queue
# --------------------------------------------------------------------------- #
def load_fixture_queue(case: dict) -> Dict[str, List[dict]]:
    """Return tool_name -> list of recorded ToolResult-shaped dicts, in call order."""
    fx = case.get("fixture")
    if not fx:
        return {}
    names = [fx] if isinstance(fx, str) else list(fx)
    queue: Dict[str, List[dict]] = {}
    for name in names:
        path = FIXTURES_DIR / name
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw["results"] if isinstance(raw, dict) and "results" in raw else [raw]
        for item in items:
            tname = item.get("tool_name") or "unknown"
            queue.setdefault(tname, []).append(item)
    return queue


_CANNED_TOOL_DATA: Dict[str, Any] = {
    "search_properties": {"success": True, "status": "no_results", "total_found": 0,
                          "recommendations": [], "summary": "(offline stub) no listings",
                          "search_criteria": {}},
    "check_safety": {"address": "(offline stub)", "safety_score": 50,
                     "safety_level": "Unknown", "crime_data": {},
                     "recommendation": "(offline stub) no crime data"},
    "search_nearby_pois": {"success": True, "address": "(offline stub)",
                           "pois": {}, "summary": "(offline stub) no POIs"},
    "calculate_commute": {"success": True, "duration_minutes": None,
                          "route_summary": "(offline stub)"},
    "calculate_commute_cost": {"success": True, "monthly_cost": None},
    "check_transport_cost": {"success": True},
    "get_transport_info": {"success": True, "summary": "(offline stub) TfL"},
    "get_weather": {"success": True, "summary": "(offline stub) weather"},
    "web_search": {"success": True, "results": "(offline stub) web results"},
    "get_property_details": {"success": True, "details": "(offline stub)"},
    "recall_memory": {"success": True, "count": 0, "memories": [], "formatted": ""},
    "remember": {"success": True, "id": "offline-stub", "stored": "", "mtype": "semantic"},
}


# --------------------------------------------------------------------------- #
# Fake-LLM script per case (offline)
# --------------------------------------------------------------------------- #
_CATALOG_INTENTS = {
    "search_properties", "market_info", "check_safety", "search_nearby_pois",
    "calculate_commute", "calculate_commute_cost", "get_transport_info",
    "get_weather", "get_property_details", "recall_memory", "web_search",
    "direct_answer",
}


def _map_intent(expected_route: Optional[str]) -> str:
    if expected_route in _CATALOG_INTENTS:
        return expected_route
    if expected_route in ("multi_search",):
        return "check_safety"   # multi_search is graph-internal; a catalog proxy
    # remember / reasoning_property / clarification are guard-driven; safe fallback
    return "direct_answer"


def build_fake_scripts(case: dict) -> Dict[str, str]:
    query = case.get("user_query", "")
    answer = f"[offline-fake responder] Processed request: {query[:140]}"
    return {
        "intent": json.dumps({"intent": _map_intent(case.get("expected_route"))}),
        "classification": json.dumps({"intent": _map_intent(case.get("expected_route"))}),
        "responder": answer,
        "synthesis": answer,
        "planner": json.dumps({
            "searches": [{"tool": "web_search", "params": {"query": f"{query} London 2025"}}],
            "reason": "offline-fake plan",
        }),
        "default": answer,
    }


# --------------------------------------------------------------------------- #
# Referent context reconstruction (multi-turn fidelity)
# --------------------------------------------------------------------------- #
_UK_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2})\b", re.IGNORECASE)
_ASSISTANT_LEADIN_RE = re.compile(
    r"^\W*(?:here'?s|here is|found|i found|this is|i'd suggest|check out|noted[—:\- ]*)\s+",
    re.IGNORECASE)


def referent_context_from_history(hist: List[dict]) -> Dict[str, Any]:
    """Reconstruct the structured referent state a REAL multi-turn session would carry
    into this turn, so deictic references ("the first one", "that studio", "from there")
    resolve exactly as they would live.

    The harness previously only concatenated the prior turns into the query TEXT, but
    the agent's guards (safety / commute / property-details) resolve referents from
    STRUCTURED state — ``extracted_context['last_results']`` (the prior search hits)
    and ``extracted_context['property_address']`` — not from free text. With that state
    absent, C1 ("commute from the first one to UCL") and its siblings fell through to a
    "please provide both endpoints" clarification even though the address was named one
    turn earlier. This rebuilds that state from the assistant turns that named a
    property/address, mirroring what the graph writes after a real search.

    Returns {} when no assistant turn names a resolvable property/address.
    """
    results: List[dict] = []
    for turn in hist or []:
        if turn.get("role") != "assistant":
            continue
        txt = (turn.get("content") or "").strip()
        if not txt:
            continue
        body = _ASSISTANT_LEADIN_RE.sub("", txt).strip()
        pcm = _UK_POSTCODE_RE.search(txt)
        # An assistant turn that names neither a postcode nor a comma-separated place
        # carries no addressable referent — skip it.
        if not pcm and "," not in body:
            continue
        name = body.split(",", 1)[0].strip().rstrip(".") if "," in body else None
        if pcm:
            after_name = body.split(",", 1)[1].strip() if "," in body else body
            pc2 = _UK_POSTCODE_RE.search(after_name)
            address = after_name[:pc2.end()].strip() if pc2 else after_name.strip()
        else:
            address = name
        rec: Dict[str, Any] = {}
        if name:
            rec["name"] = name
        if address:
            rec["address"] = address
        if rec:
            results.append(rec)
    if not results:
        return {}
    return {"last_results": results,
            "property_address": results[0].get("address") or results[0].get("name")}


# --------------------------------------------------------------------------- #
# Per-run result record
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    case_id: str
    category: str
    config: str
    mode: str
    run_id: str
    repeat: int
    route: Any = None
    tools_called: List[str] = field(default_factory=list)
    tool_call_events: List[dict] = field(default_factory=list)
    node_latencies: Dict[str, float] = field(default_factory=dict)
    model_usage: List[dict] = field(default_factory=list)
    critic_verdicts: List[dict] = field(default_factory=list)
    turn_latency_ms: Optional[float] = None
    final_answer: str = ""
    verdict: dict = field(default_factory=dict)   # CaseVerdict.to_dict()
    grounding: dict = field(default_factory=dict)
    cost_usd: Optional[float] = 0.0
    passed: bool = False
    error: Optional[str] = None
    judge: Optional[dict] = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


# --------------------------------------------------------------------------- #
# Event helpers
# --------------------------------------------------------------------------- #
def _read_new_events(events_log: Path, offset: int) -> tuple[List[dict], int]:
    """Read events appended after byte ``offset``; return (events, new_offset)."""
    if not events_log.exists():
        return [], offset
    events: List[dict] = []
    with events_log.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        new_offset = fh.tell()
    return events, new_offset


# --------------------------------------------------------------------------- #
# The core: run one case
# --------------------------------------------------------------------------- #
class CaseRunner:
    """Holds imported modules + shared config; runs cases one at a time."""

    def __init__(self, *, mode: str, cfg, state_root: Path, events_log: Path,
                 judge: bool):
        self.mode = mode          # "offline" | "live"
        self.cfg = cfg
        self.state_root = state_root
        self.events_log = events_log
        self.judge = judge
        # Imports (env already bootstrapped).
        from evaluation.metrics import collector, pricing, fake_llm
        from evaluation.metrics import graders
        import core.langgraph_agent as lg
        from core.tool_system import create_tool_registry, ToolResult
        from core.langgraph_agent import build_agent_graph, create_initial_state
        from uk_rent_agent.agent.persistence import graph_config, get_sqlite_checkpointer
        self.collector = collector
        self.pricing = pricing.load_pricing()
        self.fake_llm = fake_llm
        self.graders = graders
        self.lg = lg
        self.create_tool_registry = create_tool_registry
        self.ToolResult = ToolResult
        self.build_agent_graph = build_agent_graph
        self.create_initial_state = create_initial_state
        self.graph_config = graph_config
        self.get_sqlite_checkpointer = get_sqlite_checkpointer
        self._events_offset = events_log.stat().st_size if events_log.exists() else 0

    # ---- long-term memory (optional; ChromaDB may live in a separate venv) --- #
    def _memory_module(self):
        """Return rag.agent_memory, or None if ChromaDB isn't importable here.

        Offline mechanics do NOT require real memory (the recall_memory/remember
        tools are stubbed/fixture-replayed), so a missing ChromaDB degrades to a
        no-op rather than failing the run.
        """
        try:
            import rag.agent_memory as am
            return am
        except Exception:
            return None

    # ---- per-case state isolation ------------------------------------- #
    def _isolate_state(self, run_id: str) -> Path:
        safe = run_id.replace("#", "_").replace(":", "_")
        d = self.state_root / safe
        d.mkdir(parents=True, exist_ok=True)
        os.environ["CHECKPOINT_PATH"] = str(d / "checkpoints.sqlite3")
        os.environ["IDEMPOTENCY_DB"] = str(d / "idempotency.sqlite3")
        # Fresh ChromaDB agent-memory store per case-run.
        #
        # BUG (cross-case memory bleed): the old reset — `am._DB_PATH = <tmp>` plus
        # `am._AGENT_MEMORY = None` — did NOT isolate. `AgentMemory.__init__(self,
        # db_path=_DB_PATH)` binds the default at definition time, and
        # `get_agent_memory()` calls `AgentMemory()` with NO argument, so every case
        # rebound to the ORIGINAL on-disk store (app/chroma_db_agent_memory). All 45
        # cases (all user_id="u_alice") therefore shared one physical store, and each
        # case's conversation_history replay accumulated there — so A1 (empty history)
        # recalled "[PAST] Find me a studio near Bloomsbury" etc. from OTHER cases.
        #
        # Robust fix: (a) drop chromadb's process-global PersistentClient cache, then
        # (b) instantiate a brand-new AgentMemory bound EXPLICITLY to this run's unique
        # temp path (bypassing the def-time default). Combined with per-run user_id
        # namespacing (see run()), this makes cross-case recall impossible both
        # physically (separate store) and logically (separate user_id filter).
        am = self._memory_module()
        if am is not None:
            new_path = str(d / "chroma_agent_memory")
            am._DB_PATH = new_path
            try:
                from chromadb.api.shared_system_client import SharedSystemClient
                SharedSystemClient._identifier_to_system.clear()
            except Exception:
                pass
            try:
                am._AGENT_MEMORY = am.AgentMemory(db_path=new_path)
            except Exception:
                am._AGENT_MEMORY = None   # ChromaDB unavailable: degrade to no-op
        return d

    def _ns_uid(self, run_id: str, base_uid: Optional[str]) -> Optional[str]:
        """Namespace a case's user_id with the run_id so that, even if a memory store
        were shared across cases, the per-user ``where`` filter can never surface
        another case's records. The mapping is deterministic and applied identically to
        BOTH the case's own user_id and any seeded other_user_id, so within-case
        continuity (G_memory) and the memory-isolation no-leak check stay valid."""
        if not base_uid:
            return None
        return f"{run_id}::{base_uid}"

    def _seed_memory(self, case: dict, run_id: str) -> None:
        """For memory-isolation cases, seed the OTHER user's memory so the no-leak
        check is real; also persist prior conversation turns to long-term memory.

        Both the case user and the seeded other_user are namespaced with the run_id
        (see _ns_uid) — the SAME scheme the retrieval path uses — so isolation stays a
        genuine two-different-users test while cross-case bleed is impossible."""
        am = self._memory_module()
        if am is None:
            return
        memory = am.get_agent_memory()
        for con in case.get("expected_constraints", []):
            if con.get("type") == "memory_isolation":
                other = self._ns_uid(run_id, con.get("other_user_id"))
                val = con.get("value")
                if other and val is not None:
                    memory.add(f"User budget is £{val} per month.", "semantic",
                               session_id=other, user_id=other)
        # Replay prior turns into long-term memory (same store the graph writes to),
        # so recall/isolation semantics have real state without regenerating text.
        uid = self._ns_uid(run_id, case.get("user_id"))
        hist = case.get("conversation_history") or []
        for turn in hist:
            if turn.get("role") == "user" and uid:
                memory.add(turn.get("content", ""), "episodic",
                           session_id=uid, user_id=uid, role="user")

    def _build_query_with_history(self, case: dict, uid: Optional[str]) -> str:
        hist = case.get("conversation_history") or []
        q = case.get("user_query", "")
        if not hist:
            base = q
        else:
            lines = [f"User: {t['content']}" if t["role"] == "user"
                     else f"Alex: {t['content']}" for t in hist[-3:]]
            base = "Previous conversation:\n" + "\n".join(lines) + f"\n\nCurrent user message: {q}"
        # Inject long-term memory the way app.py does (offline-safe: temp chroma).
        # ``uid`` is the run-namespaced user_id, so retrieval only ever sees THIS
        # case-run's memory — never another case's replayed turns.
        am = self._memory_module()
        if am is not None:
            try:
                mem = am.get_agent_memory()
                mems = mem.retrieve(q, session_id=uid, user_id=uid, n=6)
                block = mem.format_for_prompt(mems)
                if block:
                    base = f"{block}\n\n{base}"
            except Exception:
                pass
        return base

    # ---- tool patch (fixture replay + evidence capture + recording) ---- #
    @contextlib.contextmanager
    def _patch_tools(self, registry, fixture_queue: Dict[str, List[dict]],
                     evidence: List[dict]):
        orig_execute = registry.execute_tool
        last_by_tool: Dict[str, dict] = {}
        offline = self.mode == "offline"
        ToolResult = self.ToolResult
        record = self.collector.record_tool_call

        def _result_from_fixture(name, item) -> Any:
            return ToolResult(
                success=bool(item.get("success", True)),
                data=item.get("data"),
                error=item.get("error"),
                tool_name=item.get("tool_name", name),
                execution_time_ms=0.5,
            )

        async def patched(name, **kwargs):
            item = None
            if name in fixture_queue and fixture_queue[name]:
                item = fixture_queue[name].pop(0)
                last_by_tool[name] = item
            elif name in last_by_tool:
                item = last_by_tool[name]  # reuse last fixture if calls exceed records

            if item is not None:
                result = _result_from_fixture(name, item)
                record(name, result, kwargs, mcp=False)
            elif offline:
                data = _CANNED_TOOL_DATA.get(name, {"success": True, "stub": True})
                result = ToolResult(success=True, data=data, error=None,
                                    tool_name=name, execution_time_ms=0.5)
                record(name, result, kwargs, mcp=False)
            else:
                # live, no fixture: real tool (records itself inside orig_execute)
                result = await orig_execute(name, **kwargs)
            evidence.append({"tool": name, "data": result.data,
                             "success": result.success, "error": result.error})
            return result

        registry.execute_tool = patched
        try:
            yield
        finally:
            registry.execute_tool = orig_execute

    # ---- fake-LLM patch (offline) or judge llm (live) ------------------ #
    @contextlib.contextmanager
    def _patch_llm(self, case: dict):
        if self.mode == "offline":
            scripts = build_fake_scripts(case)
            with self.fake_llm.patch_model_router(scripts), \
                    self.fake_llm.patch_call_ollama({"default": "{}"}):
                yield
        else:
            yield  # live: real ModelRouter (config's router override applied by caller)

    def _judge_llm(self):
        if self.mode != "live" or not self.judge:
            return None
        try:
            from uk_rent_agent.llm.router import ModelRouter
            return ModelRouter().create("judge")  # deepseek-chat, temp 0
        except Exception:
            return None

    # ---- run one case -------------------------------------------------- #
    async def run(self, case: dict, repeat: int) -> RunResult:
        case_id = case.get("case_id", "?")
        run_id = f"{case_id}#r{repeat}#{self.cfg.name}"
        rr = RunResult(case_id=case_id, category=case.get("category", "?"),
                       config=self.cfg.name, mode=self.mode, run_id=run_id, repeat=repeat)
        self._isolate_state(run_id)
        # Run-namespaced user_id: isolates memory across cases even under a shared store
        # (defense-in-depth on top of the fresh per-run ChromaDB path in _isolate_state).
        eff_uid = self._ns_uid(run_id, case.get("user_id", "u")) or "u"
        self._seed_memory(case, run_id)

        registry = self.create_tool_registry()
        fixture_queue = load_fixture_queue(case)
        evidence: List[dict] = []

        # Fresh checkpointer per case (isolated file).
        checkpointer = self.get_sqlite_checkpointer(Path(os.environ["CHECKPOINT_PATH"]))

        # Apply retrieval-concurrency (serial => max_concurrency=1) via graph config.
        gconfig = self.graph_config(eff_uid, f"conv_{case_id}",
                                    request_id=uuid.uuid4().hex)
        if self.cfg.max_concurrency is not None:
            gconfig = dict(gconfig)
            gconfig["max_concurrency"] = self.cfg.max_concurrency

        query = self._build_query_with_history(case, eff_uid)
        # Reconstruct the structured referent state (last_results / property_address)
        # that a real multi-turn session would carry, so deictic references in the
        # current message ("the first one", "that studio", "from there") resolve the
        # way they would live instead of being clarification-gated.
        extracted_context = {"current_message": case.get("user_query", "")}
        extracted_context.update(referent_context_from_history(
            case.get("conversation_history") or []))
        state = self.create_initial_state(
            user_query=query, extracted_context=extracted_context,
            user_id=eff_uid, session_id=f"conv_{case_id}",
            request_id=gconfig["configurable"].get("request_id"),
        )

        started = time.perf_counter()
        final_state = None
        run_error: Optional[str] = None
        with self.collector.capture_run(run_id, case_id, self.cfg.name):
            with self._patch_tools(registry, fixture_queue, evidence), self._patch_llm(case):
                # Build graph INSIDE the patches so build-time LLM factory (intent) is faked.
                graph = self.build_agent_graph(registry, checkpointer=checkpointer)
                try:
                    final_state = await graph.ainvoke(state, config=gconfig)
                except Exception as exc:
                    run_error = f"{type(exc).__name__}: {exc}"
                _lat = (time.perf_counter() - started) * 1000
                # Emit the turn event the app.py handler would (parity with collector's
                # documented event set); the runner supersedes app.py's turn record.
                fs = final_state or {}
                self.collector.record_turn(
                    route=fs.get("tool_decision"),
                    response_type=fs.get("response_type", "answer"),
                    critic_attempts=fs.get("critic_attempts"),
                    verdict=fs.get("verdict"),
                    latency_ms=_lat,
                )
        rr.turn_latency_ms = (time.perf_counter() - started) * 1000

        # Collect this run's events.
        events, self._events_offset = _read_new_events(self.events_log, self._events_offset)
        mine = [e for e in events if e.get("run_id") == run_id]

        rr.final_answer = (final_state or {}).get("final_response", "") if final_state else ""
        rr.route = (final_state or {}).get("tool_decision") if final_state else None
        rr.error = run_error

        tool_events = [e for e in mine if e.get("type") == "tool_call"]
        rr.tool_call_events = tool_events
        rr.tools_called = [e.get("tool") for e in tool_events]
        rr.node_latencies = {e.get("node"): e.get("latency_ms")
                             for e in mine if e.get("type") == "node_span"}
        rr.model_usage = [e for e in mine if e.get("type") == "llm_call"]
        rr.critic_verdicts = [e for e in mine if e.get("type") == "critic_verdict"]

        # Cost from llm_call events.
        rr.cost_usd = self._cost_of(rr.model_usage)

        # Grade (deterministic).
        user_texts = [t["content"] for t in (case.get("conversation_history") or [])
                      if t.get("role") == "user"]
        user_texts.append(case.get("user_query", ""))
        gctx = self.graders.GradeContext(
            final_answer=rr.final_answer,
            tools_called=rr.tools_called,
            tool_call_events=tool_events,
            evidence=evidence,
            route=rr.route,
            user_texts=user_texts,
            reference_calculations=case.get("reference_calculations"),
            error=run_error,
        )
        verdict = self.graders.grade_case(case, gctx)
        rr.verdict = verdict.to_dict()
        rr.grounding = verdict.grounding.to_dict()
        rr.passed = verdict.passed

        # Optional auxiliary judge (live only).
        judge_llm = self._judge_llm()
        if judge_llm is not None:
            rr.judge = self.graders.run_judge(case, gctx, judge_llm=judge_llm)

        return rr

    def _cost_of(self, llm_events: List[dict]) -> Optional[float]:
        total = 0.0
        any_priced = False
        for e in llm_events:
            c = self.pricing.cost(
                e.get("model", ""),
                input_tokens=e.get("input_tokens") or 0,
                output_tokens=e.get("output_tokens") or 0,
                cached_tokens=e.get("cached_tokens") or 0,
            )
            if c is not None:
                total += c
                any_priced = True
        return total if any_priced else 0.0


# --------------------------------------------------------------------------- #
# Result writers
# --------------------------------------------------------------------------- #
def _percentile(values: List[float], pct: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def write_raw_runs(out: Path, runs: List[RunResult]) -> None:
    with (out / "raw_runs.jsonl").open("w", encoding="utf-8") as fh:
        for rr in runs:
            fh.write(json.dumps(rr.to_dict(), ensure_ascii=False, default=str) + "\n")


def write_per_case(out: Path, runs: List[RunResult]) -> None:
    with (out / "per_case.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "category", "config", "repeat", "passed", "task_completed",
                    "grounded_rate", "money_grounded_rate", "source_coverage",
                    "constraints_passed", "constraints_total", "tools_ok",
                    "tools_called", "route", "latency_ms", "cost_usd", "error"])
        for rr in runs:
            v = rr.verdict
            g = rr.grounding
            route = rr.route.get("tool") if isinstance(rr.route, dict) else rr.route
            w.writerow([
                rr.case_id, rr.category, rr.config, rr.repeat,
                rr.passed, v.get("task_completed"),
                _fmt(g.get("grounded_rate")), _fmt(g.get("money_grounded_rate")),
                _fmt(g.get("source_coverage")),
                v.get("constraints_passed"), v.get("constraints_total"), v.get("tools_ok"),
                "|".join(rr.tools_called), route,
                _fmt(rr.turn_latency_ms), _fmt(rr.cost_usd), rr.error or "",
            ])


def write_tool_metrics(out: Path, tool_events: List[dict]) -> None:
    by_tool: Dict[str, List[dict]] = {}
    for e in tool_events:
        by_tool.setdefault(e.get("tool", "?"), []).append(e)
    with (out / "tool_metrics.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tool", "calls", "success", "fail", "timeout", "retry",
                    "empty_result", "mean_latency_ms", "p50_latency_ms", "p95_latency_ms"])
        for tool, evs in sorted(by_tool.items()):
            lats = [e.get("execution_time_ms") for e in evs if e.get("execution_time_ms") is not None]
            w.writerow([
                tool, len(evs),
                sum(1 for e in evs if e.get("success")),
                sum(1 for e in evs if not e.get("success")),
                sum(1 for e in evs if e.get("timeout")),
                sum((e.get("retry_count") or 0) for e in evs),
                sum(1 for e in evs if e.get("empty_result")),
                _fmt(mean(lats) if lats else None),
                _fmt(_percentile(lats, 0.5)),
                _fmt(_percentile(lats, 0.95)),
            ])


def write_model_usage(out: Path, runs: List[RunResult], pricing) -> None:
    with (out / "model_usage.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["run_id", "case_id", "model", "purpose", "input_tokens",
                    "output_tokens", "cached_tokens", "cost_usd", "latency_ms", "success"])
        for rr in runs:
            for e in rr.model_usage:
                cost = pricing.cost(
                    e.get("model", ""),
                    input_tokens=e.get("input_tokens") or 0,
                    output_tokens=e.get("output_tokens") or 0,
                    cached_tokens=e.get("cached_tokens") or 0,
                )
                w.writerow([
                    rr.run_id, rr.case_id, e.get("model"), e.get("purpose"),
                    e.get("input_tokens"), e.get("output_tokens"), e.get("cached_tokens"),
                    _fmt(cost), _fmt(e.get("latency_ms")), e.get("success"),
                ])


def write_summary(out: Path, runs: List[RunResult], *, mode: str, cfg_name: str,
                  repeats: int, cost_cap: float, stopped_reason: Optional[str],
                  n_selected: int, timestamp: str) -> dict:
    n = len(runs)
    passed = sum(1 for r in runs if r.passed)
    completed = sum(1 for r in runs if r.verdict.get("task_completed"))
    # grounding aggregates (denominators!)
    tot_claims = sum(r.grounding.get("total_verifiable_claims", 0) for r in runs)
    grounded = sum(r.grounding.get("grounded_claims", 0) for r in runs)
    money_tot = sum(r.grounding.get("money_total", 0) for r in runs)
    money_grounded = sum(r.grounding.get("money_grounded", 0) for r in runs)
    contradicted = sum(r.grounding.get("contradicted", 0) for r in runs)
    sourced = sum(r.grounding.get("sourced_claims", 0) for r in runs)
    con_pass = sum(r.verdict.get("constraints_passed", 0) for r in runs)
    con_tot = sum(r.verdict.get("constraints_total", 0) for r in runs)
    critic_triggers = sum(len(r.critic_verdicts) for r in runs)
    critic_repairs = sum(1 for r in runs for v in r.critic_verdicts
                         if v.get("stage") == "regenerated")
    latencies = [r.turn_latency_ms for r in runs if r.turn_latency_ms is not None]
    total_in = sum((e.get("input_tokens") or 0) for r in runs for e in r.model_usage)
    total_out = sum((e.get("output_tokens") or 0) for r in runs for e in r.model_usage)
    total_cost = sum((r.cost_usd or 0.0) for r in runs)

    def ratio(num, den):
        return {"num": num, "den": den, "display": f"{num}/{den}",
                "rate": (num / den if den else None)}

    summary = {
        "config": cfg_name,
        "mode": mode,
        "offline": mode == "offline",
        "repeats": repeats,
        "n_cases_selected": n_selected,
        "n_runs": n,
        "passed": ratio(passed, n),
        "task_completion": ratio(completed, n),
        "constraints": ratio(con_pass, con_tot),
        "grounded_rate": ratio(grounded, tot_claims),
        "money_grounded_rate": ratio(money_grounded, money_tot),
        "contradicted_claims": contradicted,
        "source_coverage": ratio(sourced, tot_claims),
        "critic_triggers": critic_triggers,
        "critic_repairs": critic_repairs,
        "latency_ms": {
            "mean": mean(latencies) if latencies else None,
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "n": len(latencies),
        },
        "tokens": {"input": total_in, "output": total_out},
        "total_cost_usd": total_cost,
        "cost_cap_usd": cost_cap,
        "stopped_reason": stopped_reason,
        "git_commit": _git_commit(),
        "timestamp": timestamp,
        "notes": ("OFFLINE mode validates MECHANICS ONLY (routing/tool/latency/"
                  "memory-isolation); grounding numbers use FAKE responder text and are "
                  "NOT a measure of real grounding quality."
                  if mode == "offline" else
                  "LIVE mode: fixtured cases replay recorded evidence; non-fixture cases "
                  "run tools in-process (may hit cache/live network, nondeterministic)."),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
    return summary


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _git_commit() -> Optional[str]:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Checkpoint (resume)
# --------------------------------------------------------------------------- #
def _load_checkpoint(out: Path) -> dict:
    p = out / "_checkpoint.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_checkpoint(out: Path, done_run_ids: List[str], cumulative_cost: float,
                     stopped_reason: Optional[str]) -> None:
    (out / "_checkpoint.json").write_text(json.dumps({
        "done_run_ids": done_run_ids,
        "cumulative_cost_usd": cumulative_cost,
        "stopped_reason": stopped_reason,
    }, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main async driver
# --------------------------------------------------------------------------- #
async def _run_all(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    events_log = out / "events.jsonl"
    state_root = Path(tempfile.mkdtemp(prefix="rc_eval_state_"))

    _bootstrap_env(state_root, events_log)

    from evaluation.configs.loader import load_config, apply_config
    from evaluation.metrics import pricing as pricing_mod

    cfg = load_config(args.config)
    cases = load_cases()
    problems = schema_validate(cases)
    if problems:
        print("Schema problems (first 10):")
        for p in problems[:10]:
            print(f"  - {p}")
        return 2
    selected = select_cases(cases, smoke=args.smoke, limit=args.limit, category=args.category)
    if not selected:
        print("No cases selected.")
        return 1

    timestamp = args.timestamp or time.strftime("%Y-%m-%dT%H:%M:%S")
    mode = "offline" if args.offline else "live"
    pricing = pricing_mod.load_pricing()

    # Fresh run: truncate the event log so events don't accumulate across invocations
    # (resume keeps the existing log, appending new cases).
    if not args.resume and events_log.exists():
        events_log.unlink()

    ckpt = _load_checkpoint(out) if args.resume else {}
    done_ids = set(ckpt.get("done_run_ids", []))
    cumulative_cost = float(ckpt.get("cumulative_cost_usd", 0.0))

    # Load prior runs (resume) so writers include them.
    runs: List[RunResult] = []
    if args.resume and (out / "raw_runs.jsonl").exists():
        for line in (out / "raw_runs.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                runs.append(_runresult_from_dict(d))

    stopped_reason: Optional[str] = None
    total_units = len(selected) * args.repeat
    completed_units = len(runs)

    with apply_config(cfg):
        runner = CaseRunner(mode=mode, cfg=cfg, state_root=state_root,
                            events_log=events_log, judge=args.judge)
        for repeat in range(1, args.repeat + 1):
            for case in selected:
                run_id = f"{case.get('case_id')}#r{repeat}#{cfg.name}"
                if run_id in done_ids:
                    continue
                # Cost cap: refuse to START a case that could exceed the cap.
                if mode == "live" and completed_units > 0:
                    est_per_case = cumulative_cost / max(completed_units, 1)
                    if cumulative_cost + est_per_case > args.max_cost_usd:
                        stopped_reason = (f"cost cap reached: stopped after "
                                          f"{completed_units}/{total_units} runs "
                                          f"(cumulative ${cumulative_cost:.4f}, "
                                          f"est next ${est_per_case:.4f}, cap ${args.max_cost_usd})")
                        print(stopped_reason)
                        break
                print(f"[run] {run_id} ({mode})", flush=True)
                rr = await runner.run(case, repeat)
                runs.append(rr)
                done_ids.add(run_id)
                cumulative_cost += (rr.cost_usd or 0.0)
                completed_units += 1
                # Persist incremental results + checkpoint after EACH case.
                write_raw_runs(out, runs)
                _save_checkpoint(out, list(done_ids), cumulative_cost, stopped_reason)
                if cumulative_cost > args.max_cost_usd:
                    stopped_reason = (f"cost cap reached: stopped after "
                                      f"{completed_units}/{total_units} runs "
                                      f"(cumulative ${cumulative_cost:.4f} > cap ${args.max_cost_usd})")
                    print(stopped_reason)
                    break
            if stopped_reason:
                break

    # Final writers.
    all_tool_events = [e for rr in runs for e in rr.tool_call_events]
    write_raw_runs(out, runs)
    write_per_case(out, runs)
    write_tool_metrics(out, all_tool_events)
    write_model_usage(out, runs, pricing)
    if args.judge:
        _write_judge_io(out, runs)
    summary = write_summary(out, runs, mode=mode, cfg_name=cfg.name, repeats=args.repeat,
                            cost_cap=args.max_cost_usd, stopped_reason=stopped_reason,
                            n_selected=len(selected), timestamp=timestamp)
    _save_checkpoint(out, list(done_ids), cumulative_cost, stopped_reason)

    print("\n=== summary ===")
    print(f"config={summary['config']} mode={summary['mode']} "
          f"runs={summary['n_runs']} passed={summary['passed']['display']} "
          f"task_completed={summary['task_completion']['display']} "
          f"grounded={summary['grounded_rate']['display']} "
          f"money_grounded={summary['money_grounded_rate']['display']} "
          f"cost=${summary['total_cost_usd']:.4f}")
    # Cleanup temp state (keep results).
    with contextlib.suppress(Exception):
        shutil.rmtree(state_root, ignore_errors=True)
    return 0


def _runresult_from_dict(d: dict) -> RunResult:
    rr = RunResult(case_id=d.get("case_id", "?"), category=d.get("category", "?"),
                   config=d.get("config", "?"), mode=d.get("mode", "?"),
                   run_id=d.get("run_id", "?"), repeat=d.get("repeat", 1))
    for k, v in d.items():
        if hasattr(rr, k):
            setattr(rr, k, v)
    return rr


def _write_judge_io(out: Path, runs: List[RunResult]) -> None:
    with (out / "judge_io.jsonl").open("w", encoding="utf-8") as fh:
        for rr in runs:
            if rr.judge is not None:
                fh.write(json.dumps(rr.judge, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evaluation.run_benchmark",
                                description="RentCompass offline eval runner.")
    p.add_argument("--config", default="routed_models",
                   help="config name or path (default: routed_models)")
    p.add_argument("--smoke", action="store_true", help="only smoke cases")
    p.add_argument("--limit", type=int, default=None, help="cap number of cases")
    p.add_argument("--category", default=None, help="filter by category (e.g. A_retrieval or A)")
    p.add_argument("--repeat", type=int, default=1, help="repeat each case K times")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--offline", action="store_true", help="fake-LLM, unbilled (default)")
    grp.add_argument("--live", action="store_true", help="real DeepSeek (PAID)")
    p.add_argument("--max-cost-usd", type=float, default=15.0,
                   help="hard cost cap in USD (¥110≈$15; see README FX). Default 15.0")
    p.add_argument("--judge", action="store_true", help="enable auxiliary LLM judge (live only)")
    p.add_argument("--resume", action="store_true", help="skip runs already in checkpoint")
    p.add_argument("--out", default="evaluation/results", help="output directory")
    p.add_argument("--timestamp", default=None, help="timestamp string for summary.json")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.live:
        args.offline = True  # default to offline/unbilled
    if args.live and args.offline:
        raise SystemExit("choose --offline or --live, not both")
    return asyncio.run(_run_all(args))


if __name__ == "__main__":
    raise SystemExit(main())
