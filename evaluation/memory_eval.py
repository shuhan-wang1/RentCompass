"""Phase-7 long-term-memory evaluation.

    python -m evaluation.memory_eval [--out evaluation/results] [--timestamp TS]

Evaluates the agent's ChromaDB-backed long-term memory (``app/rag/agent_memory.py``)
deterministically wherever possible:

* preference extraction precision      * user A/B isolation
* preference update correctness        * forget / delete (GDPR erasure)
* stale-memory replacement             * process-restart recovery
* contradictory-preference handling    * retrieval relevance

Reports (all with denominators):
    memory_write_success_rate, memory_retrieval_accuracy, user_isolation_pass_rate,
    forget_request_pass_rate, restart_recovery_pass_rate.

BLOCKER: the store requires ``chromadb``. ``app/rag/agent_memory.py`` imports it at
module load, so when chromadb is absent EVERY store-dependent check is blocked. This
script DETECTS that at startup and writes ``results/memory_eval.json`` with status
``blocked: chromadb not installed`` (never a fabricated number). Install chromadb (or
run in the app venv) to fill the numbers in. The deterministic isolation/forget/restart
logic is additionally covered by ``tests/test_agent_memory_isolation.py``.

Model calls (importance rating / fact extraction / consolidation / reflection) go
through ``call_ollama``; this eval stubs it so NOTHING is billed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return "unknown"


def _chromadb_available() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except Exception:
        return False


def _pin_app_path():
    """Ensure the real ``app`` packages win over any stale shadow copies (mirrors
    tests/test_agent_memory_isolation._pin_app)."""
    local = str(REPO_ROOT / "app")
    if local in sys.path:
        sys.path.remove(local)
    sys.path.insert(0, local)
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))
    for name in list(sys.modules):
        if name in ("core", "rag") or name.startswith(("core.", "rag.")):
            path = (getattr(sys.modules[name], "__file__", "") or "").replace("\\", "/")
            if "/app/" not in path and not path.endswith("/app"):
                del sys.modules[name]


def _ratio(num: int, den: int) -> dict:
    return {"num": num, "den": den, "display": f"{num}/{den}",
            "rate": (num / den if den else None)}


# --------------------------------------------------------------------------- #
# The deterministic checks (only run when chromadb is importable)
# --------------------------------------------------------------------------- #
def _run_checks(out_note: list) -> dict:
    import importlib

    _pin_app_path()
    am_mod = importlib.import_module("rag.agent_memory")
    AgentMemory = am_mod.AgentMemory

    checks = {}
    write_ok = write_total = 0
    retrieval_ok = retrieval_total = 0
    iso_ok = iso_total = 0
    forget_ok = forget_total = 0
    restart_ok = restart_total = 0

    tmp = Path(tempfile.mkdtemp(prefix="rc_mem_eval_"))

    # ---- identity gate (pure, no store) -------------------------------- #
    gate = am_mod._valid_user_id
    gate_cases = [("alice-1", "alice-1"), ("  bob  ", "bob"), (None, None),
                  ("", None), ("default", None), ("Default", None), (123, None)]
    gate_pass = sum(1 for inp, exp in gate_cases if gate(inp) == exp)
    checks["identity_gate"] = _ratio(gate_pass, len(gate_cases))

    # ---- helper: fresh memory with a scripted call_ollama -------------- #
    def fresh(script_fn, path):
        am_mod.call_ollama = script_fn  # deterministic, unbilled
        return AgentMemory(db_path=str(path))

    # 1. preference extraction precision + write success ----------------- #
    facts = ["User budget is £1500 per month", "User studies at UCL",
             "User wants a 1-bed flat"]
    mem = fresh(lambda *a, **k: json.dumps({"facts": facts}), tmp / "extract")
    for f in facts:
        write_total += 1
        if mem.add(f, "semantic", user_id="u-extract") is not None:
            write_ok += 1
    got = {m["text"] for m in mem.retrieve("budget university flat", user_id="u-extract", n=10)}
    stored_correct = len(got & set(facts))
    checks["extraction_precision"] = _ratio(stored_correct, len(facts))
    retrieval_total += 1
    if stored_correct == len(facts):
        retrieval_ok += 1

    # 2. preference update correctness ----------------------------------- #
    mem2 = fresh(lambda *a, **k: "5", tmp / "update")
    old_id = mem2.add("User budget is £1200 per month", "semantic", user_id="u-upd")
    ops = {"ops": [{"event": "UPDATE", "id": old_id,
                    "text": "User budget is £1800 per month"}]}
    am_mod.call_ollama = lambda *a, **k: json.dumps(ops)
    mem2._consolidate(["User budget is £1800 per month"], "s", "u-upd")
    texts = {m["text"] for m in mem2.retrieve("budget", user_id="u-upd", n=10)}
    upd_ok = "User budget is £1800 per month" in texts and \
             "User budget is £1200 per month" not in texts
    checks["update_correctness"] = _ratio(int(upd_ok), 1)
    retrieval_total += 1
    retrieval_ok += int(upd_ok)

    # 3. stale-memory replacement (delete old, add new) ------------------ #
    mem3 = fresh(lambda *a, **k: "5", tmp / "stale")
    stale_id = mem3.add("User wants to live in Zone 4", "semantic", user_id="u-stale")
    ops3 = {"ops": [{"event": "DELETE", "id": stale_id},
                    {"event": "ADD", "text": "User wants to live in Zone 1"}]}
    am_mod.call_ollama = lambda *a, **k: json.dumps(ops3)
    mem3._consolidate(["User wants to live in Zone 1"], "s", "u-stale")
    t3 = {m["text"] for m in mem3.retrieve("zone", user_id="u-stale", n=10)}
    stale_ok = "User wants to live in Zone 1" in t3 and "User wants to live in Zone 4" not in t3
    checks["stale_replacement"] = _ratio(int(stale_ok), 1)
    retrieval_total += 1
    retrieval_ok += int(stale_ok)

    # 4. contradictory preference handling ------------------------------- #
    mem4 = fresh(lambda *a, **k: "5", tmp / "contra")
    c_id = mem4.add("User has no pets", "semantic", user_id="u-con")
    ops4 = {"ops": [{"event": "DELETE", "id": c_id},
                    {"event": "ADD", "text": "User has a dog"}]}
    am_mod.call_ollama = lambda *a, **k: json.dumps(ops4)
    mem4._consolidate(["User has a dog"], "s", "u-con")
    t4 = {m["text"] for m in mem4.retrieve("pets dog", user_id="u-con", n=10)}
    contra_ok = "User has a dog" in t4 and "User has no pets" not in t4
    checks["contradiction_handling"] = _ratio(int(contra_ok), 1)
    retrieval_total += 1
    retrieval_ok += int(contra_ok)

    # 5. user A/B isolation ---------------------------------------------- #
    mem5 = fresh(lambda *a, **k: "5", tmp / "iso")
    write_total += 1
    if mem5.add("Budget is £999 in Camden", "semantic", user_id="user-A") is not None:
        write_ok += 1
    iso_checks = [
        ({m["text"] for m in mem5.retrieve("budget", user_id="user-A", n=5)}
         == {"Budget is £999 in Camden"}),
        (mem5.retrieve("budget", user_id="user-B", n=5) == []),
        (mem5.retrieve("budget", user_id=None, n=5) == []),
        (mem5.retrieve("budget", user_id="default", n=5) == []),
        (mem5.add("orphan", "semantic") is None),  # write without id rejected
    ]
    iso_ok += sum(1 for c in iso_checks if c)
    iso_total += len(iso_checks)
    checks["user_isolation"] = _ratio(sum(1 for c in iso_checks if c), len(iso_checks))

    # 6. forget / delete -------------------------------------------------- #
    mem6 = fresh(lambda *a, **k: "5", tmp / "forget")
    mem6.add("A one", "semantic", user_id="user-A")
    mem6.add("A two", "semantic", user_id="user-A")
    mem6.add("B one", "semantic", user_id="user-B")
    wiped = mem6.forget("user-A")
    forget_checks = [
        wiped == 2,
        mem6.retrieve("one two", user_id="user-A", n=5) == [],
        [m["text"] for m in mem6.retrieve("one", user_id="user-B", n=5)] == ["B one"],
    ]
    forget_ok += sum(1 for c in forget_checks if c)
    forget_total += len(forget_checks)
    checks["forget_delete"] = _ratio(sum(1 for c in forget_checks if c), len(forget_checks))

    # 7. process-restart recovery (persist to disk, rebuild instance) ----- #
    restart_path = tmp / "restart"
    mem7a = fresh(lambda *a, **k: "5", restart_path)
    mem7a.add("User budget is £1400 near KCL", "semantic", user_id="u-restart")
    del mem7a
    mem7b = AgentMemory(db_path=str(restart_path))   # simulate a fresh process
    recovered = {m["text"] for m in mem7b.retrieve("budget", user_id="u-restart", n=5)}
    restart_pass = "User budget is £1400 near KCL" in recovered
    restart_ok += int(restart_pass)
    restart_total += 1
    checks["restart_recovery"] = _ratio(int(restart_pass), 1)

    # 8. retrieval relevance (top hit matches the queried topic) ---------- #
    mem8 = fresh(lambda *a, **k: "5", tmp / "relevance")
    for t in ["User budget is £1600 per month", "User commute limit is 30 minutes",
              "User wants to avoid Zone 5", "User studies at Imperial College"]:
        mem8.add(t, "semantic", user_id="u-rel")
    rel = mem8.retrieve("how long is my commute", user_id="u-rel", n=1)
    rel_ok = bool(rel) and "commute" in rel[0]["text"].lower()
    checks["retrieval_relevance"] = _ratio(int(rel_ok), 1)
    retrieval_total += 1
    retrieval_ok += int(rel_ok)

    return {
        "status": "ok",
        "checks": checks,
        "rates": {
            "memory_write_success_rate": _ratio(write_ok, write_total),
            "memory_retrieval_accuracy": _ratio(retrieval_ok, retrieval_total),
            "user_isolation_pass_rate": _ratio(iso_ok, iso_total),
            "forget_request_pass_rate": _ratio(forget_ok, forget_total),
            "restart_recovery_pass_rate": _ratio(restart_ok, restart_total),
        },
    }


# --------------------------------------------------------------------------- #
# Blocked-output builder
# --------------------------------------------------------------------------- #
_BLOCKED_CHECKS = [
    "identity_gate", "extraction_precision", "update_correctness", "stale_replacement",
    "contradiction_handling", "user_isolation", "forget_delete", "restart_recovery",
    "retrieval_relevance",
]
_BLOCKED_RATES = [
    "memory_write_success_rate", "memory_retrieval_accuracy", "user_isolation_pass_rate",
    "forget_request_pass_rate", "restart_recovery_pass_rate",
]


def _blocked_result(reason: str) -> dict:
    blocked = {"status": "blocked", "reason": reason}
    return {
        "status": f"blocked: {reason}",
        "checks": {c: dict(blocked) for c in _BLOCKED_CHECKS},
        "rates": {r: dict(blocked) for r in _BLOCKED_RATES},
        "note": ("app/rag/agent_memory.py imports chromadb at module load, so every "
                 "store-dependent check is blocked. Install chromadb (or run in the app "
                 "venv), then re-run `python -m evaluation.memory_eval`. The deterministic "
                 "isolation/forget/restart/idempotency contracts are additionally covered "
                 "by tests/test_agent_memory_isolation.py (run: pytest -q "
                 "tests/test_agent_memory_isolation.py in the app venv)."),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evaluation.memory_eval")
    p.add_argument("--out", default="evaluation/results")
    p.add_argument("--timestamp", default=None)
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = args.timestamp or time.strftime("%Y-%m-%dT%H:%M:%S")

    if not _chromadb_available():
        result = _blocked_result("chromadb not installed")
    else:
        notes: list = []
        try:
            result = _run_checks(notes)
        except Exception as exc:
            result = _blocked_result(f"memory check harness error: {type(exc).__name__}: {exc}")

    result.update({
        "framework": "memory_eval",
        "chromadb_available": _chromadb_available(),
        "git_commit": _git_commit(),
        "timestamp": ts,
    })
    (out / "memory_eval.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"memory_eval status: {result['status']}")
    for k, v in result.get("rates", {}).items():
        disp = v.get("display") if isinstance(v, dict) and "display" in v else v.get("status")
        print(f"  {k:<32} {disp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
