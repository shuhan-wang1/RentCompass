"""Paired A/B runner for the 2026-08-04 unattended evaluation (GOAL §实验A / §实验C).

Lives under evaluation/results/** because the GOAL's hard boundary allows writes only
there and to PROGRESS.log. It imports the REPO's existing evaluation harness
(evaluation.run_benchmark.CaseRunner) and the app's own model-router/client
constructors -- no new config layer, no hardcoded key.

What it adds over evaluation/run_ablation.py:

  * ``--arch fc_loop``  -- run_ablation has no --arch flag and therefore measures the
    LEGACY graph. The GOAL requires the CURRENT app/core/agent_loop.py::build_fc_graph.
  * PAIRED execution -- for one case, both arms run back-to-back (same case, same
    reconstructed input, same restored listing-cache snapshot) so a per-case difference
    is a within-pair difference.
  * PER-RUN append to JSONL (crash-safe / resumable) instead of an in-memory aggregate.
  * "strong model call" is recorded by THINKING MODE, not by model name. Under the
    current router chat_model == reasoner_model == deepseek-v4-flash and the tiers
    differ only in extra_body {"thinking": ...}; a name-based count (which is what
    run_ablation._is_strong does) would report 100% strong in BOTH arms.
  * An optional single-worker tool-offload pool (``--serial-tools``) for Experiment C:
    a pure SCHEDULING change (same batch, same tool calls, same evidence) analogous to
    what evaluation/configs/serial_retrieval.yaml does for the legacy graph via
    LangGraph max_concurrency=1 -- which has no effect on the fc loop, whose batch
    concurrency comes from the thread pool in agent_loop._tool_offload_executor.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (REPO_ROOT, REPO_ROOT / "app", REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# --------------------------------------------------------------------------- #
# thinking-mode tagging
# --------------------------------------------------------------------------- #
# ModelRouter.create() resolves a ModelRoute (which carries `reasoning`) and then hands
# only (model_name, purpose) to the collector. We need `reasoning` on the llm_call event
# to count strong (thinking) calls, so create() stashes the route it just resolved and a
# wrapped instrument_chat_model folds it into the purpose string as "<purpose>#think" /
# "<purpose>#nothink". Nothing in app/ is edited; both patches are process-local.
_LAST_ROUTE = {"reasoning": None, "model": None}


def _install_thinking_tag() -> None:
    from uk_rent_agent.llm import router as _router
    from evaluation.metrics import collector as _collector

    if getattr(_router.ModelRouter, "_ab_tagged", False):
        return
    orig_create = _router.ModelRouter.create
    orig_instrument = _collector.instrument_chat_model

    def _create(self, purpose, *, base_url=None, **route_kwargs):
        try:
            route = self.route(purpose, **route_kwargs)
            _LAST_ROUTE["reasoning"] = bool(route.reasoning)
            _LAST_ROUTE["model"] = route.model
        except Exception:
            _LAST_ROUTE["reasoning"] = None
            _LAST_ROUTE["model"] = None
        return orig_create(self, purpose, base_url=base_url, **route_kwargs)

    def _instrument(model, *, provider, model_name, purpose=None):
        r = _LAST_ROUTE.get("reasoning")
        if r is not None and purpose is not None:
            purpose = f"{purpose}#{'think' if r else 'nothink'}"
        return orig_instrument(model, provider=provider, model_name=model_name,
                               purpose=purpose)

    _router.ModelRouter.create = _create
    _router.ModelRouter._ab_tagged = True
    _collector.instrument_chat_model = _instrument


# --------------------------------------------------------------------------- #
# "all nodes on the strong MODEL" arm (Experiment A baseline, second design)
# --------------------------------------------------------------------------- #
# evaluation/configs/baseline_all_strong.yaml forces reasoning=True everywhere. On this
# commit that is NOT a model change (chat_model == reasoner_model == deepseek-v4-flash),
# it is a THINKING-MODE change -- and DeepSeek rejects a thinking-mode follow-up whose
# prior assistant turn's `reasoning_content` was not echoed back, which the fc loop's
# message builder does not do. That arm therefore dies with HTTP 400 on the deeper tool
# loops (measured; see PROGRESS.log). The runnable "strong model at every node" baseline
# is deepseek-v4-pro with the SAME (non-thinking) message protocol as production, so the
# contrast is model strength alone. Temperature / max_tokens per purpose are preserved.
ARM_ALL_PRO = "baseline_all_pro"


@contextlib.contextmanager
def _patch_router_all_pro():
    from uk_rent_agent.llm import router as _router

    original = _router.ModelRouter.route

    def _all_pro(self, purpose, *, complex_task=False, low_latency=False):
        base = original(self, purpose, complex_task=complex_task, low_latency=low_latency)
        return _router.ModelRoute(model=self.pro_model, temperature=base.temperature,
                                  max_tokens=base.max_tokens, reasoning=False)

    _router.ModelRouter.route = _all_pro
    try:
        yield
    finally:
        _router.ModelRouter.route = original


# --------------------------------------------------------------------------- #
# serial tool dispatch (Experiment C control arm)
# --------------------------------------------------------------------------- #
def _force_single_tool_worker(n_workers: int = 1) -> None:
    """Pin agent_loop's tool-offload pool to ``n_workers``.

    agent_loop._tool_offload_executor() floors the FC_TOOL_OFFLOAD_WORKERS env at
    max(4, workers), so the env alone cannot serialise the batch. This replaces the
    module-level singleton with a 1-worker pool, which makes the reads in one batch run
    one-after-another while EVERYTHING else (batch composition, params, evidence, budgets)
    is unchanged -- the pure scheduling contrast Experiment C is about.
    """
    from concurrent.futures import ThreadPoolExecutor
    import core.agent_loop as al

    if getattr(al, "_TOOL_OFFLOAD_EXECUTOR", None) is not None:
        with contextlib.suppress(Exception):
            al._TOOL_OFFLOAD_EXECUTOR.shutdown(wait=False)
    al._TOOL_OFFLOAD_EXECUTOR = ThreadPoolExecutor(
        max_workers=n_workers, thread_name_prefix="fc_tool_serial")


def _reset_tool_pool() -> None:
    import core.agent_loop as al
    with contextlib.suppress(Exception):
        if getattr(al, "_TOOL_OFFLOAD_EXECUTOR", None) is not None:
            al._TOOL_OFFLOAD_EXECUTOR.shutdown(wait=False)
    al._TOOL_OFFLOAD_EXECUTOR = None


# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _progress(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")
        fh.flush()


def _done_run_ids(path: Path) -> set:
    done = set()
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = d.get("ab_run_key")
            if rid:
                done.add(rid)
    return done


def _tool_stats(rr) -> dict:
    ev = rr.tool_call_events or []
    ok = sum(1 for e in ev if e.get("success"))
    return {"tool_calls": len(ev), "tool_ok": ok, "tool_fail": len(ev) - ok}


async def _run_one(runner, case, repeat, timeout_s):
    started = time.perf_counter()
    try:
        rr = await asyncio.wait_for(runner.run(case, repeat), timeout=timeout_s)
        return rr, None
    except asyncio.TimeoutError:
        return None, f"harness_timeout_{timeout_s}s"
    except Exception as exc:  # noqa: BLE001 - one bad case must not kill the sweep
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        del started


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--arms", default="baseline_all_strong,routed_models",
                   help="comma-separated eval config names, in pairing order")
    p.add_argument("--arch", default="fc_loop", choices=["fc_loop", "legacy"])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--shard-index", type=int, default=0)
    p.add_argument("--shard-count", type=int, default=1)
    p.add_argument("--cases", default=None, help="path to a case shard jsonl")
    p.add_argument("--case-ids", default=None, help="comma-separated case_id allowlist")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", required=True, help="output dir (evaluation/results/...)")
    p.add_argument("--runs-file", default="runs.jsonl")
    p.add_argument("--progress-log", default=str(REPO_ROOT / "PROGRESS.log"))
    p.add_argument("--experiment", default="A")
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument("--gap-ms", type=float, default=300.0)
    p.add_argument("--max-runs", type=int, default=10_000)
    p.add_argument("--max-consecutive-failures", type=int, default=10)
    p.add_argument("--deadline", default=None,
                   help="local ISO time after which NO new run is started")
    p.add_argument("--cache-snapshot", default=None)
    p.add_argument("--cold-cache", action="store_true",
                   help="COLD protocol: a brand-new EMPTY listing cache per run (every "
                        "search_properties call does real work). Mutually exclusive with "
                        "--cache-snapshot.")
    p.add_argument("--cache-ttl-hours", default="8760")
    p.add_argument("--serial-tools", action="store_true",
                   help="Experiment C control arm: pin the tool-offload pool to 1 worker")
    p.add_argument("--serial-tools-arm", default=None,
                   help="apply --serial-tools ONLY to this arm name (paired C design)")
    args = p.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runs_path = out / args.runs_file
    prog = Path(args.progress_log)
    tag = f"{args.experiment}#s{args.shard_index}"

    # TTL must be pinned before any app import (on_demand reads it at import time).
    if args.cache_snapshot:
        os.environ["SEARCH_CACHE_TTL_HOURS"] = str(args.cache_ttl_hours)
    os.environ["AGENT_ARCH"] = args.arch

    from evaluation.run_benchmark import (CaseRunner, load_cases, _bootstrap_env)
    from evaluation.configs.loader import load_config, apply_config

    state_root = Path(tempfile.mkdtemp(prefix=f"rc_ab_{args.experiment}_"))
    events_log = out / f"events_shard{args.shard_index}.jsonl"
    _bootstrap_env(state_root, events_log)
    os.environ["AGENT_ARCH"] = args.arch          # _bootstrap_env must not clobber it
    _install_thinking_tag()

    if not os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY") == "offline-eval-placeholder":
        _progress(prog, f"{_now_iso()} [{tag}] ABORT: DEEPSEEK_API_KEY not loaded "
                        f"(app/.env missing or unreadable); refusing to run with a placeholder key")
        print("ABORT: no DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    if args.cache_snapshot and args.cold_cache:
        raise SystemExit("choose --cache-snapshot or --cold-cache, not both")
    cache_protocol = {"mode": "none"}
    if args.cold_cache:
        cache_protocol = {"mode": "cold", "snapshot_path": None, "snapshot_sha256": None,
                          "restored_per_repeat": True}
    if args.cache_snapshot:
        import hashlib
        snap = Path(args.cache_snapshot)
        h = hashlib.sha256()
        with snap.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
        sidecar = snap.with_name(snap.name + ".sha256")
        expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
        if expected != sha:
            raise SystemExit(f"snapshot sha256 mismatch: {snap}")
        cache_protocol = {"mode": "warm", "snapshot_path": str(snap),
                          "snapshot_sha256": sha, "restored_per_repeat": True,
                          "ttl_hours_env": str(args.cache_ttl_hours)}

    cases = load_cases(Path(args.cases) if args.cases else None)
    if args.case_ids:
        allow = {c.strip() for c in args.case_ids.split(",") if c.strip()}
        cases = [c for c in cases if c.get("case_id") in allow]
    if args.limit:
        cases = cases[:args.limit]
    shard = [c for i, c in enumerate(cases) if i % args.shard_count == args.shard_index]

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    # ARM_ALL_PRO / the two Experiment-C arms reuse the UNPATCHED production config and
    # install their own process-local override (route table for A, tool-offload pool for C);
    # every other arm names a config in evaluation/configs/.
    _PRODUCTION_CFG_ARMS = {ARM_ALL_PRO, "serial_tools", "parallel_tools"}
    cfgs = {a: load_config("routed_models" if a in _PRODUCTION_CFG_ARMS else a) for a in arms}
    deadline = datetime.fromisoformat(args.deadline).timestamp() if args.deadline else None

    done = _done_run_ids(runs_path)
    _progress(prog, f"{_now_iso()} [{tag}] START arch={args.arch} arms={arms} "
                    f"repeats={args.repeats} cases={len(shard)} resumed_runs={len(done)} "
                    f"cache={cache_protocol['mode']} serial_tools_arm={args.serial_tools_arm} "
                    f"timeout={args.timeout_s}s deadline={args.deadline}")

    async def sweep() -> int:
        consecutive_failures = 0
        n_started = 0
        for case in shard:
            cid = case.get("case_id")
            for rep in range(1, args.repeats + 1):
                # Alternate which arm goes first so any residual order effect
                # (provider-side warm-up, background load) is balanced across arms.
                order = arms if (rep % 2 == 1) else list(reversed(arms))
                for pos, arm in enumerate(order):
                    key = f"{cid}#r{rep}#{arm}"
                    if key in done:
                        continue
                    if deadline and time.time() >= deadline:
                        _progress(prog, f"{_now_iso()} [{tag}] STOP deadline reached "
                                        f"before {key}")
                        return 0
                    if n_started >= args.max_runs:
                        _progress(prog, f"{_now_iso()} [{tag}] STOP max_runs "
                                        f"{args.max_runs} reached before {key}")
                        return 0

                    serial = bool(args.serial_tools
                                  or (args.serial_tools_arm and arm == args.serial_tools_arm)
                                  or arm == "serial_tools")
                    if serial:
                        _force_single_tool_worker(1)
                    else:
                        _reset_tool_pool()

                    cfg = cfgs[arm]
                    t_start = _now_iso()
                    t0 = time.perf_counter()
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(apply_config(cfg))
                        if arm == ARM_ALL_PRO:
                            stack.enter_context(_patch_router_all_pro())
                        runner = CaseRunner(mode="live", cfg=cfg, state_root=state_root,
                                            events_log=events_log, judge=False,
                                            arch=args.arch,
                                            cache_protocol=dict(cache_protocol))
                        rr, err = await _run_one(runner, case, rep, args.timeout_s)
                    wall_ms = (time.perf_counter() - t0) * 1000.0
                    n_started += 1

                    if rr is None:
                        rec = {"ab_run_key": key, "case_id": cid,
                               "category": case.get("category"), "arm": arm,
                               "repeat": rep, "arch": args.arch, "mode": "live",
                               "ab_error": err, "ab_ok": False,
                               "ab_started_at": t_start, "ab_finished_at": _now_iso(),
                               "ab_wall_ms": wall_ms, "ab_order_pos": pos,
                               "ab_serial_tools": serial, "shard": args.shard_index}
                        consecutive_failures += 1
                    else:
                        rec = rr.to_dict()
                        rec.update({"ab_run_key": key, "arm": arm, "arch": args.arch,
                                    "ab_error": rr.error, "ab_ok": rr.error is None,
                                    "ab_started_at": t_start, "ab_finished_at": _now_iso(),
                                    "ab_wall_ms": wall_ms, "ab_order_pos": pos,
                                    "ab_serial_tools": serial, "shard": args.shard_index})
                        rec.update(_tool_stats(rr))
                        consecutive_failures = 0 if rr.error is None else consecutive_failures + 1

                    with runs_path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                        fh.flush()
                    done.add(key)

                    ok = rec.get("ab_ok")
                    _progress(prog, f"{_now_iso()} [{tag}] {key} arm={arm} "
                                    f"{'OK' if ok else 'FAIL'} {wall_ms:.0f}ms "
                                    f"llm={rec.get('llm_calls', 0)} "
                                    f"tools={rec.get('tool_calls', 0)} "
                                    f"cost={rec.get('cost_usd')} "
                                    f"err={rec.get('ab_error')}")

                    if consecutive_failures >= args.max_consecutive_failures:
                        _progress(prog, f"{_now_iso()} [{tag}] ABORT experiment: "
                                        f"{consecutive_failures} consecutive failures")
                        return 3
                    await asyncio.sleep(args.gap_ms / 1000.0)
        return 0

    rc = asyncio.run(sweep())
    _progress(prog, f"{_now_iso()} [{tag}] END rc={rc} runs_file={runs_path}")
    with contextlib.suppress(Exception):
        import shutil
        shutil.rmtree(state_root, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
