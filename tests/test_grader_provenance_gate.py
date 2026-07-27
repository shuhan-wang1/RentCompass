"""The re-score identity gate must key on the GRADER, not only on the case contract.

WHAT WAS BROKEN
---------------
``evaluation/rescore.py`` computed ``grader_sha256`` in ``_evaluator_identity`` and put it
in the report, and its identity gate refused a run only on ``case_contract_sha256``. So the
evaluator's own identity was stamped where a reader could find it and asserted on nowhere —
HANDOFF §0's defect class verbatim.

It bit for real. In the 2026-07-24 grader repair the SAME retained evidence scored fc 59/98
under one grader and 74/98 under the next, with ``case_contract_sha256`` byte-identical for
part of that move (HANDOFF §3.14/§3.15). And it is on disk: ``/home/shuhan/fp-results/``
holds one paired round, ``idp98_r{1,2,3}_{base,cand}``, where the base arms recorded
``grader_sha256`` ``c25a027d04…`` and the cand arms ``4cf33e0553…`` — ONE contract
(``7f1ead524c…``), TWO graders — and ``rescore.py`` printed their ``scored`` columns side by
side and exited 0.

WHAT THESE TESTS PIN
--------------------
1. a provable grader mismatch is REFUSED, on the same footing as a contract mismatch;
2. a manifest that cannot prove which evaluator scored it reads ``UNKNOWN``, never
   ``match`` — the third-answer idiom ``_dirty_word`` already uses for the working tree;
3. the round of record and every other retained package stay RE-SCORABLE (back-compat);
4. the hashed boundary is the actual verdict-determining file set, re-derived here from the
   import graph so the pinned list cannot silently drift (a source guard, not a promise);
5. ``critic.py`` really can flip a verdict with ``graders.py`` byte-identical — the
   measurement that decides the boundary, run rather than asserted.

Test 1 FAILS on the pre-fix tree behaviourally, not just by signature: the pre-fix
``rescore.main`` exits 0 and reports both arms re-scorable.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "app", REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evaluation import results_package as rp  # noqa: E402
from evaluation import rescore  # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures: the smallest run dir the gate accepts
# --------------------------------------------------------------------------- #
CASE = {"case_id": "A1", "expected_constraints": []}
CONTRACT = "7f1ead524c421e33f4098afff036f019a92537d5f1f76deba59580aa34dc6907"

# The two grader hashes the real idp98 arms recorded. Pinned as literals so this test keeps
# describing the round that actually exists on disk (practice 1: pin the observed value).
IDP98_BASE_GRADER = "c25a027d04e227aeb30969600aa5a8e5fc087d5237e8acd1b629925b115e0495"
IDP98_CAND_GRADER = "4cf33e055357fd738c85eee1d1955e662c659ad8a8579abd609a880cf3eda460"


def _rec(case_id="A1", passed=True):
    import hashlib
    ev = [{"tool": "search_properties", "success": True, "error": None, "data": {"x": 1}}]
    blob = json.dumps(ev, ensure_ascii=False, sort_keys=True, default=str)
    return {"run_id": f"{case_id}#1", "case_id": case_id, "repeat": 1,
            "raw_evidence_sha256": hashlib.sha256(blob.encode()).hexdigest(),
            "evidence": ev,
            "grader_input": {"final_answer": "ok", "tools_called": ["search_properties"],
                             "tool_call_events": [], "route": None, "user_texts": [],
                             "reference_calculations": None, "error": None,
                             "reconstructed_context": None, "history_texts": []},
            "scored_passed": passed, "scored_route_matched": True}


def _run_dir(tmp_path, name, manifest, records=(_rec(),)):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if records is not None:
        (d / "grader_input.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return d


def _arm(grader_sha256=None, **extra):
    m = {"product_sha": "8793c0b", "capture_sha": "8793c0b",
         "case_contract_sha256": CONTRACT}
    if grader_sha256 is not None:
        m["grader_sha256"] = grader_sha256
    m.update(extra)
    return m


def _evaluator():
    ident = rescore._evaluator_identity()
    ident["case_contract_sha256"] = CONTRACT
    return ident


# =========================================================================== #
# 1. THE REGRESSION. A grader change now gets caught.
# =========================================================================== #
def test_a_grader_mismatch_is_refused_like_a_contract_mismatch(tmp_path):
    """The pre-fix gate accepted this run: product, capture and contract all agree with the
    evaluator and only the GRADER differs, which was the one thing it never looked at."""
    d = _run_dir(tmp_path, "cand", _arm(grader_sha256=IDP98_CAND_GRADER))
    r = rescore.rescore_dir(d, {"A1": CASE}, None,
                            expected_contract=CONTRACT, expected_grader=_evaluator())
    assert r["rescorable"] is False
    assert "IDENTITY REFUSED" in r["reason"]
    assert "DIFFERENT grader" in r["reason"]
    assert r["grader_identity"] == rp.GRADER_MISMATCH
    # and the same run is accepted the moment only the contract is checked — i.e. this is
    # exactly the hole, not some other defect.
    ok = rescore.rescore_dir(d, {"A1": CASE}, _FakeGraders(),
                             expected_contract=CONTRACT, expected_grader=None)
    assert ok["rescorable"] is True and ok["grader_identity"] == rp.GRADER_UNKNOWN


def test_the_cli_exits_nonzero_when_the_arms_recorded_under_two_graders(tmp_path, capsys):
    """END-TO-END, and the behavioural failure on the pre-fix tree: this is the shape of the
    real idp98 round — one contract, two graders, one paired A/B. The pre-fix ``main``
    printed both arms' ``scored`` columns beside each other and returned 0."""
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps(CASE) + "\n", encoding="utf-8")
    contract = __import__("hashlib").sha256(cases.read_bytes()).hexdigest()
    base = _run_dir(tmp_path, "idp98_r1_base",
                    _arm(grader_sha256=IDP98_BASE_GRADER, case_contract_sha256=contract))
    cand = _run_dir(tmp_path, "idp98_r1_cand",
                    _arm(grader_sha256=IDP98_CAND_GRADER, case_contract_sha256=contract))
    out_json = tmp_path / "report.json"
    argv = ["evaluation.rescore", "--runs", str(base), str(cand),
            "--cases", str(cases), "--out", str(out_json)]
    old = sys.argv
    sys.argv = argv
    try:
        rc = rescore.main()
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert rc == 1, "two arms recorded under two graders must not exit 0"
    assert "DIFFERENT grader" in out
    report = json.loads(out_json.read_text())
    assert report["arms_agree_on_grader"] is False
    assert sorted(report["recorded_grader_keys"].values()) == sorted(
        [f"legacy-graders.py:{IDP98_BASE_GRADER}", f"legacy-graders.py:{IDP98_CAND_GRADER}"])


def test_the_evaluators_own_grader_identity_is_now_asserted_not_just_stamped():
    """The defect in one line: every field the evaluator stamps must be reachable by the
    gate. ``grader_set_sha256`` is what ``rescore_dir`` compares; ``grader_sha256`` is kept
    only as the legacy partial witness."""
    ident = _evaluator()
    assert ident["grader_set_sha256"] and ident["grader_sha256"]
    assert ident["grader_sha256"] == ident["grader_set_files"][rp.GRADER_SET_PRIMARY]
    matching = rp.compare_grader_identity(
        {"grader_set_sha256": ident["grader_set_sha256"]}, ident)
    assert matching["grader_identity"] == rp.GRADER_MATCH
    drifted = rp.compare_grader_identity({"grader_set_sha256": "0" * 64}, ident)
    assert drifted["grader_identity"] == rp.GRADER_MISMATCH


# =========================================================================== #
# 2 + 3. BACK-COMPAT: absent is UNKNOWN, and UNKNOWN is not a refusal
# =========================================================================== #
class _FakeGraders:
    """Grades everything as passing. The gate, not the checkers, is under test here."""

    class GradeContext:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _V:
        passed = True

        def to_dict(self):
            return {"constraints": []}

    @classmethod
    def grade_case(cls, case, ctx):
        return cls._V()


def test_a_missing_grader_hash_reads_UNKNOWN_and_never_match(tmp_path):
    """The whole point of the third state: silence must not be promoted to agreement."""
    d = _run_dir(tmp_path, "no_grader_field", _arm())
    r = rescore.rescore_dir(d, {"A1": CASE}, _FakeGraders(),
                            expected_contract=CONTRACT, expected_grader=_evaluator())
    assert r["rescorable"] is True, "refusing every retained package is a gate nobody keeps"
    assert r["grader_identity"] == rp.GRADER_UNKNOWN
    assert r["grader_identity"] != rp.GRADER_MATCH
    assert "NOT thereby a match" in r["grader_detail"]
    assert r["recorded_grader_key"] == "undeclared"


def test_a_legacy_graders_py_hash_that_matches_is_still_only_UNKNOWN(tmp_path):
    """A legacy manifest proves graders.py is byte-identical and says nothing about the
    other seven files in the set, so it is PARTIALLY witnessed — reported as such."""
    ident = _evaluator()
    d = _run_dir(tmp_path, "legacy_match",
                 _arm(grader_sha256=ident["grader_set_files"][rp.GRADER_SET_PRIMARY]))
    r = rescore.rescore_dir(d, {"A1": CASE}, _FakeGraders(),
                            expected_contract=CONTRACT, expected_grader=ident)
    assert r["rescorable"] is True
    assert r["grader_identity"] == rp.GRADER_UNKNOWN
    assert "PARTIALLY witnessed" in r["grader_detail"]


def test_the_round_of_record_is_still_rescorable(tmp_path):
    """`.runtime/round-8793c0b-internal-2026-07-25/` is THE round of record and predates
    ``grader_set_sha256``. Adding the grader leg must not make it unreadable — that is why
    UNKNOWN is not a refusal. Guards the same promise
    ``test_eval_writer_commit_identity.py`` already makes for the commit fields."""
    manifest = {"argv": ["python", "-m", "evaluation.run_benchmark"], "mode": "live",
                "product_sha": "8793c0b17963a6a2b375903a164d3d96395dc834",
                "capture_sha": None, "evaluator_sha": None, "capture_is_product": False,
                "case_contract_sha256": CONTRACT,
                "timestamp": "2026-07-25T14:02:21",
                "git_commit": None, "git_dirty": None}
    d = _run_dir(tmp_path, "round_of_record", manifest)
    r = rescore.rescore_dir(d, {"A1": CASE}, _FakeGraders())
    assert r["rescorable"] is True
    assert r["capture_sha"] == r["product_sha"]
    assert r["grader_identity"] == rp.GRADER_UNKNOWN


def test_one_arm_alone_never_trips_the_cross_arm_check(tmp_path):
    runs = [{"run_dir": "solo", "recorded_grader_key": f"legacy-graders.py:{IDP98_BASE_GRADER}"}]
    assert rescore._cross_arm_grader_check(runs)["arms_agree_on_grader"] is True


def test_arms_that_agree_pass_the_cross_arm_check():
    key = f"set:{'a' * 64}"
    runs = [{"run_dir": "a", "recorded_grader_key": key},
            {"run_dir": "b", "recorded_grader_key": key}]
    assert rescore._cross_arm_grader_check(runs)["arms_agree_on_grader"] is True


def test_compare_grader_identity_never_raises_on_any_shape():
    for rec in (None, {}, "nope", 7, {"grader_sha256": None},
                {"grader_set_sha256": "x", "grader_set_files": None}):
        for ev in (None, {}, _evaluator()):
            assert rp.compare_grader_identity(rec, ev)["grader_identity"] in (
                rp.GRADER_MATCH, rp.GRADER_MISMATCH, rp.GRADER_UNKNOWN)


def test_a_mismatch_names_the_file_that_moved(tmp_path):
    """A gate that only says "different" gets argued with. One that says WHICH file gets
    acted on — and it is what keeps the raw-bytes boundary usable in review."""
    ident = _evaluator()
    stale = dict(ident["grader_set_files"])
    stale["src/uk_rent_agent/agent/critic.py"] = "f" * 64
    r = rp.compare_grader_identity(
        {"grader_set_sha256": "0" * 64, "grader_set_files": stale}, ident)
    assert r["grader_identity"] == rp.GRADER_MISMATCH
    assert r["grader_set_files_differing"] == ["src/uk_rent_agent/agent/critic.py"]
    assert "critic.py" in r["grader_detail"]


# =========================================================================== #
# 4. SOURCE GUARD on the boundary itself
# =========================================================================== #
def _repo_module_file(mod: str) -> Path | None:
    """Resolve a dotted module to a repo-local file, mirroring ``rescore._bootstrap``'s
    sys.path order (app, src, repo root)."""
    parts = mod.split(".")
    for root in (REPO_ROOT / "app", REPO_ROOT / "src", REPO_ROOT):
        pkg = root.joinpath(*parts) / "__init__.py"
        if pkg.exists():
            return pkg
        mod_file = root.joinpath(*parts[:-1]) / (parts[-1] + ".py")
        if mod_file.exists():
            return mod_file
    return None


def _import_closure(entry: str) -> set[str]:
    """Repo-local transitive import closure of ``entry``, from the AST — so FUNCTION-LOCAL
    imports count (``graders`` reaches ``uk_rent_agent.agent.critic`` through one) and a new
    one cannot be added without this guard noticing.

    Ancestor packages are part of the closure because importing a submodule executes every
    ``__init__.py`` above it: that is the ONLY reason ``state.py`` is verdict-determining —
    ``uk_rent_agent/agent/__init__.py`` does ``from .state import AgentState``, and nothing
    on the grading path names ``state`` directly.
    """
    seen: set[str] = set()
    queue = [entry]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        path = _repo_module_file(mod)
        if path is None:
            continue
        seen.add(mod)
        parts = mod.split(".")
        queue.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        # Relative imports resolve against the module's own package.
        pkg = mod if path.name == "__init__.py" else ".".join(parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                queue.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = pkg if node.level else ""
                if node.level > 1:
                    base = ".".join(pkg.split(".")[:-(node.level - 1)])
                target = f"{base}.{node.module}" if (base and node.module) else (
                    node.module or base)
                if not target:
                    continue
                queue.append(target)
                queue.extend(f"{target}.{a.name}" for a in node.names)
    files = set()
    for mod in seen:
        p = _repo_module_file(mod)
        if p is not None:
            files.add(str(p.relative_to(REPO_ROOT)).replace("\\", "/"))
    return files


def test_the_hashed_set_is_the_real_verdict_determining_closure():
    """SOURCE GUARD, not a promise: re-derive the closure and fail if GRADER_SET_FILES has
    drifted from it. Add a function-local import to a checker and this test — not a future
    reader — is what notices that the verdict surface just got wider."""
    derived = _import_closure("evaluation.metrics.graders")
    pinned = set(rp.GRADER_SET_FILES)
    assert derived == pinned, (
        "GRADER_SET_FILES no longer equals the import closure of the grading path.\n"
        f"  reachable but NOT hashed (verdicts can change unseen): {sorted(derived - pinned)}\n"
        f"  hashed but NOT reachable (the gate cries wolf):        {sorted(pinned - derived)}")


def test_every_hashed_file_exists_and_the_digest_covers_all_of_them():
    ident = rp.grader_set_identity(REPO_ROOT)
    assert set(ident["grader_set_files"]) == set(rp.GRADER_SET_FILES)
    assert all(v for v in ident["grader_set_files"].values()), "a hashed file is missing"
    assert ident["grader_set_algo"] == rp.GRADER_SET_ALGO
    # Reproducible with sha256sum + sort, which is the reason this is raw bytes.
    import hashlib
    payload = "".join(f"{rel} {ident['grader_set_files'][rel]}\n"
                      for rel in sorted(rp.GRADER_SET_FILES))
    assert ident["grader_set_sha256"] == hashlib.sha256(payload.encode()).hexdigest()


def test_a_deleted_checker_module_changes_the_digest(tmp_path):
    """An absent file hashes as None and still occupies a line, so shrinking the set is a
    change, not a silent narrowing."""
    for rel in rp.GRADER_SET_FILES:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((REPO_ROOT / rel).read_bytes())
    full = rp.grader_set_identity(tmp_path)["grader_set_sha256"]
    (tmp_path / "src/uk_rent_agent/agent/critic.py").unlink()
    assert rp.grader_set_identity(tmp_path)["grader_set_sha256"] != full


def test_the_new_manifest_carries_the_grader_set_so_future_rounds_are_not_UNKNOWN(tmp_path):
    """Otherwise every round from here on is UNKNOWN forever — the same "computed, never
    used" shape one level up."""
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "A1"}\n', encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text('{"type": "node_span"}\n', encoding="utf-8")
    m = rp.build_manifest(argv=["python"], arch="fc_loop", config="routed_models",
                          timestamp="2026-07-27T00:00:00", case_file=case_file,
                          events_log=events, mode="live",
                          git_commit=lambda: "cap1", git_dirty=lambda: False)
    assert m["grader_set_sha256"] == rp.grader_set_identity()["grader_set_sha256"]
    assert m["grader_sha256"] == m["grader_set_files"][rp.GRADER_SET_PRIMARY]
    ident = _evaluator()
    assert rp.compare_grader_identity(m, ident)["grader_identity"] == rp.GRADER_MATCH


# =========================================================================== #
# 5. THE MEASUREMENT THAT DECIDES THE BOUNDARY
# =========================================================================== #
def test_critic_can_flip_a_verdict_while_graders_py_is_byte_identical(monkeypatch):
    """Why a bare ``graders.py`` hash is TOO NARROW, executed rather than asserted.

    ``_c_no_false_retrieval_provenance`` takes its denial cues AND its usability predicate
    from ``uk_rent_agent.agent.critic``. Neutralise ``claims_no_retrieval`` and the
    constraint flips FAIL→PASS on byte-identical evidence with ``graders.py`` untouched.
    The constraint is live in ``cases_guard_regression.jsonl``, so this is a reachable
    verdict, not a hypothetical one.
    """
    import hashlib
    from evaluation.metrics import graders
    from uk_rent_agent.agent import critic

    graders_sha = hashlib.sha256(
        (REPO_ROOT / rp.GRADER_SET_PRIMARY).read_bytes()).hexdigest()
    ctx = graders.GradeContext(
        final_answer="很抱歉，我没有搜索到任何数据。", tools_called=["web_search"],
        tool_call_events=[],
        evidence=[{"tool": "web_search", "data": {"results": [
            {"title": "Hackney rent report 2026",
             "snippet": "Average 1-bed rent is 1850 pcm",
             "url": "https://example.com/a"}]}}],
        route=None, user_texts=["what is rent in hackney"], reference_calculations=None,
        error=None, reconstructed_context=None, history_texts=[])
    con = {"type": "no_false_retrieval_provenance"}
    assert graders._c_no_false_retrieval_provenance(con, ctx).passed is False

    monkeypatch.setattr(critic, "claims_no_retrieval", lambda *a, **k: False)
    assert graders._c_no_false_retrieval_provenance(con, ctx).passed is True, (
        "critic.py no longer determines this verdict — re-derive GRADER_SET_FILES")
    assert hashlib.sha256(
        (REPO_ROOT / rp.GRADER_SET_PRIMARY).read_bytes()).hexdigest() == graders_sha
    # …and the boundary that would have missed it is exactly the legacy field.
    assert rp.GRADER_SET_PRIMARY in rp.GRADER_SET_FILES
    assert "src/uk_rent_agent/agent/critic.py" in rp.GRADER_SET_FILES


# =========================================================================== #
# the evaluator's own tree state: UNKNOWN, not "clean"
# =========================================================================== #
def test_the_evaluator_never_reports_an_unread_tree_as_clean(monkeypatch):
    """``_evaluator_identity`` used to run ``bool(git status --porcelain stdout.strip())``,
    which maps "git could not answer" onto ``False`` — a CLEAN claim about a tree it never
    read. That is the normal condition in the bench container. Observed on 2026-07-27:
    ``evaluator_sha: null`` printed beside ``evaluator_dirty: false``."""
    monkeypatch.setattr(rp, "probe_git", lambda *a, **k: (None, None))
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    ident = rescore._evaluator_identity()
    assert ident["evaluator_sha"] is None
    assert ident["evaluator_dirty"] is None, "an unread tree must not read as clean"
    assert ident["evaluator_commit_trust"] == rp.TRUST_UNKNOWN
    assert ident["evaluator_commit_source"] == rp.SOURCE_UNAVAILABLE
    assert ident["evaluator_identity_warnings"]


def test_rescore_no_longer_rolls_its_own_git_probe():
    """It was the last file exempted from the git-probe source guard."""
    src = (REPO_ROOT / "evaluation" / "rescore.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    doc_ids = {id(n.value) for n in ast.walk(tree)
               if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    offenders = [n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in doc_ids
                 and (n.value == "git" or "rev-parse" in n.value
                      or "--porcelain" in n.value)]
    assert not offenders, offenders
    from tests import test_eval_writer_commit_identity as guard
    assert "rescore.py" not in guard.GIT_PROBE_EXEMPT


# =========================================================================== #
# the real stored rounds, read-only, when this checkout can see them
# =========================================================================== #
IDP98 = Path("/home/shuhan/fp-results")


@pytest.mark.parametrize("base,cand", [("idp98_r1_base", "idp98_r1_cand"),
                                       ("idp98_r2_base", "idp98_r2_cand"),
                                       ("idp98_r3_base", "idp98_r3_cand")])
def test_the_real_paired_round_really_did_record_two_graders(base, cand):
    """Primary source, not a report about it. Skips in the container, where
    ``/home/shuhan/fp-results`` is not mounted."""
    bm, cm = IDP98 / base / "manifest.json", IDP98 / cand / "manifest.json"
    if not (bm.exists() and cm.exists()):
        pytest.skip("retained fp-results round not mounted here")
    b, c = json.loads(bm.read_text()), json.loads(cm.read_text())
    assert b["case_contract_sha256"] == c["case_contract_sha256"], "one contract"
    assert b["grader_sha256"] != c["grader_sha256"], "two graders"
    assert b["grader_sha256"] == IDP98_BASE_GRADER
    assert c["grader_sha256"] == IDP98_CAND_GRADER
    assert not rescore._cross_arm_grader_check([
        {"run_dir": base, "recorded_grader_key": rp.recorded_grader_key(b)},
        {"run_dir": cand, "recorded_grader_key": rp.recorded_grader_key(c)},
    ])["arms_agree_on_grader"]
