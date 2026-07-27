"""Every result writer under ``evaluation/`` must be able to name the commit that produced
it — and must never claim more than it actually knows.

``run_benchmark.py`` was converted to ``results_package.resolve_commit_identity`` on
2026-07-26. FIVE writers were left behind and kept their own git-only probes:

    evaluation/report.py                 recorded "unknown"
    evaluation/run_ablation.py           recorded "unknown"  (x2 studies)
    evaluation/memory_eval.py            recorded "unknown"
    evaluation/fault_injection/run.py    recorded "unknown"
    evaluation/make_cache_snapshot.py    recorded git_dirty: FALSE — the dangerous one

The last is worse than the rest and is pinned hardest below. ``bool(_git([...]))`` maps
"git could not answer" onto ``False``, so the sidecar asserted a CLEAN TREE whenever the
probe had not looked at all. A null reads as missing; ``False`` reads as verified.

"git is unavailable" is the NORMAL condition here, not an edge case: the harness runs in a
container off a bind mount, and a git worktree's ``.git`` is a FILE pointing at a host path
that does not exist inside it. Every test here therefore drives the no-git path explicitly.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "app", REPO_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evaluation import results_package as rp  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def no_git(monkeypatch):
    """Simulate the container: git cannot answer. Patches the ONE probe every writer now
    shares, which is only possible because they all go through results_package."""
    monkeypatch.setattr(rp, "probe_git", lambda *a, **k: (None, None))


def git_says(monkeypatch, commit, dirty):
    monkeypatch.setattr(rp, "probe_git", lambda *a, **k: (commit, dirty))


IDENTITY_FIELDS = ("git_commit", "git_dirty", "git_commit_source", "git_dirty_source",
                   "commit_trust", "identity_warnings", "self_identifying")


def assert_full_identity(doc, where):
    missing = [f for f in IDENTITY_FIELDS if f not in doc]
    assert not missing, f"{where} does not record {missing}"


# The five writers, and the module attribute each now uses instead of its own probe.
WRITER_MODULES = [
    "evaluation.report",
    "evaluation.run_ablation",
    "evaluation.memory_eval",
    "evaluation.fault_injection.run",
    "evaluation.make_cache_snapshot",
]


# =========================================================================== #
# make_cache_snapshot — THE PRIORITY: a false "clean" is worse than a null
# =========================================================================== #
def _tiny_cache(path: Path) -> Path:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE listings (id TEXT PRIMARY KEY, payload TEXT)")
        db.execute("INSERT INTO listings VALUES ('a', '{}')")
    return path


def _freeze(tmp_path, argv_extra=()):
    from evaluation import make_cache_snapshot as mcs
    src = _tiny_cache(tmp_path / "src.sqlite3")
    out = tmp_path / "snap" / "warm.sqlite3"
    assert mcs.main(["--from", str(src), "--out", str(out), *argv_extra]) == 0
    meta = json.loads((out.parent / (out.name + ".meta.json")).read_text(encoding="utf-8"))
    return meta["provenance"]


def test_cache_snapshot_never_reports_a_clean_tree_it_did_not_look_at(tmp_path, monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR (this is the whole point of the file).

    Old: ``"git_dirty": bool(_git(["status", "--porcelain"]))`` -> ``bool(None)`` ->
    ``False``. The sidecar swore the tree was clean when no git had run. Rule 5 is "build
    only from clean checkouts", so a fabricated clean flag launders an unverifiable freeze
    into a compliant-looking one."""
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    prov = _freeze(tmp_path)

    # The literal defect, pinned by identity so no truthiness can hide it.
    assert prov["git_dirty"] is not False, (
        "make_cache_snapshot recorded git_dirty=False with NO git available — it asserted "
        "'clean' about a tree it never inspected")
    assert prov["git_dirty"] is None
    assert prov["git_commit"] is None
    assert prov["git_commit_source"] == rp.SOURCE_UNAVAILABLE
    assert prov["git_dirty_source"] == rp.SOURCE_UNAVAILABLE
    assert prov["commit_trust"] == rp.TRUST_UNKNOWN
    assert prov["self_identifying"] is False
    assert any("NO COMMIT BINDING" in w for w in prov["identity_warnings"])
    assert_full_identity(prov, "make_cache_snapshot provenance")


def test_cache_snapshot_still_reports_a_genuinely_clean_tree_as_clean(tmp_path, monkeypatch):
    """The fix must not be "always say unknown": when git DOES answer, False still means
    clean and the trust is the quiet one."""
    git_says(monkeypatch, "abc1234", False)
    prov = _freeze(tmp_path)
    assert prov["git_commit"] == "abc1234"
    assert prov["git_dirty"] is False
    assert prov["commit_trust"] == rp.TRUST_CLEAN
    assert prov["identity_warnings"] == []


def test_cache_snapshot_shouts_when_the_tree_was_dirty(tmp_path, monkeypatch):
    git_says(monkeypatch, "abc1234", True)
    prov = _freeze(tmp_path)
    assert prov["git_dirty"] is True
    assert prov["commit_trust"] == rp.TRUST_DIRTY
    assert any("DIRTY TREE" in w for w in prov["identity_warnings"])


def test_cache_snapshot_records_product_sha_as_asserted_not_as_observed(tmp_path, monkeypatch):
    """No git, but the operator pinned PRODUCT_SHA. The old code threw that away and wrote
    a null commit next to a fabricated clean flag. It is now recorded — and labelled as an
    ASSERTION, with dirtiness left unknown, because an env var cannot see a working tree."""
    no_git(monkeypatch)
    monkeypatch.setenv("PRODUCT_SHA", "8793c0b17963")
    prov = _freeze(tmp_path)
    assert prov["git_commit"] == "8793c0b17963"
    assert prov["git_commit_source"] == rp.SOURCE_ENV
    assert prov["commit_trust"] == rp.TRUST_ASSERTED
    assert prov["git_dirty"] is None, "an asserted SHA cannot report on the working tree"
    assert any("OPERATOR-ASSERTED" in w for w in prov["identity_warnings"])


def test_cache_snapshot_console_line_does_not_print_clean_for_an_unread_tree(
        tmp_path, monkeypatch, capsys):
    """The printed summary carried the same lie: ``' (DIRTY)' if git_dirty else ' (clean)'``
    rendered None as "(clean)"."""
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    _freeze(tmp_path)
    out = capsys.readouterr().out
    assert "(clean)" not in out
    assert rp.TRUST_UNKNOWN in out
    assert "WARNING" in out


# =========================================================================== #
# fault_injection / memory_eval / run_ablation / report — the four null writers
# =========================================================================== #
def test_fault_summary_records_the_commit_and_its_provenance(monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR: ``_git_commit()`` returned the string "unknown" here,
    discarding a PRODUCT_SHA that was sitting right there in the environment."""
    from evaluation.fault_injection import run as fault_run
    no_git(monkeypatch)
    monkeypatch.setenv("PRODUCT_SHA", "8793c0b17963")
    summary = fault_run._aggregate([], "2026-07-27T00:00:00")
    assert summary["git_commit"] == "8793c0b17963"
    assert summary["git_commit"] != "unknown"
    assert summary["git_commit_source"] == rp.SOURCE_ENV
    assert summary["commit_trust"] == rp.TRUST_ASSERTED
    assert_full_identity(summary, "fault_summary.json")


def test_fault_summary_degrades_without_git_or_product_sha(monkeypatch):
    from evaluation.fault_injection import run as fault_run
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    summary = fault_run._aggregate([], "2026-07-27T00:00:00")  # must not raise
    assert summary["git_commit"] is None and summary["self_identifying"] is False
    assert summary["commit_trust"] == rp.TRUST_UNKNOWN


def test_memory_eval_records_the_commit_and_its_provenance(tmp_path, monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR: memory_eval.json carried ``"git_commit": "unknown"``."""
    from evaluation import memory_eval
    no_git(monkeypatch)
    monkeypatch.setenv("PRODUCT_SHA", "8793c0b17963")
    # Force the blocked branch: this test is about the identity trailer, not chromadb.
    monkeypatch.setattr(memory_eval, "_chromadb_available", lambda: False)
    assert memory_eval.main(["--out", str(tmp_path)]) == 0
    doc = json.loads((tmp_path / "memory_eval.json").read_text(encoding="utf-8"))
    assert doc["git_commit"] == "8793c0b17963" and doc["git_commit"] != "unknown"
    assert doc["git_commit_source"] == rp.SOURCE_ENV
    assert doc["commit_trust"] == rp.TRUST_ASSERTED
    assert doc["git_dirty"] is None
    assert_full_identity(doc, "memory_eval.json")


def test_memory_eval_degrades_without_git_or_product_sha(tmp_path, monkeypatch):
    from evaluation import memory_eval
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    monkeypatch.setattr(memory_eval, "_chromadb_available", lambda: False)
    assert memory_eval.main(["--out", str(tmp_path)]) == 0
    doc = json.loads((tmp_path / "memory_eval.json").read_text(encoding="utf-8"))
    assert doc["git_commit"] is None and doc["commit_trust"] == rp.TRUST_UNKNOWN


def _run_model_study(tmp_path, monkeypatch, out=None):
    """Drive the real ``_study_model`` result-dict construction with zero cases, so the
    trailer written to ablation_model.json is the genuine one and nothing is executed."""
    from evaluation import run_ablation

    async def _no_runs(*a, **k):
        return []

    monkeypatch.setattr(run_ablation, "_drive_config", _no_runs)
    out = out or tmp_path
    return asyncio.run(run_ablation._study_model(
        {"_percentile": lambda xs, q: None}, [], "offline", 1, out, tmp_path,
        tmp_path / "events.jsonl", {}, set(), 0.0, "2026-07-27T00:00:00"))


def test_ablation_records_the_commit_and_its_provenance(tmp_path, monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR: ablation_model.json carried ``"git_commit": "unknown"``."""
    no_git(monkeypatch)
    monkeypatch.setenv("PRODUCT_SHA", "8793c0b17963")
    result = _run_model_study(tmp_path, monkeypatch)
    assert result["git_commit"] == "8793c0b17963" and result["git_commit"] != "unknown"
    assert result["git_commit_source"] == rp.SOURCE_ENV
    assert result["commit_trust"] == rp.TRUST_ASSERTED
    assert_full_identity(result, "ablation_model.json")
    on_disk = json.loads((tmp_path / "ablation_model.json").read_text(encoding="utf-8"))
    assert on_disk["git_commit"] == "8793c0b17963"


def test_ablation_degrades_without_git_or_product_sha(tmp_path, monkeypatch):
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    result = _run_model_study(tmp_path, monkeypatch)  # must not raise
    assert result["git_commit"] is None and result["commit_trust"] == rp.TRUST_UNKNOWN


def test_report_head_states_provenance_instead_of_the_word_unknown(monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR: ``_git_commit()`` printed "unknown" with no provenance,
    and never consulted PRODUCT_SHA."""
    from evaluation import report
    no_git(monkeypatch)
    monkeypatch.setenv("PRODUCT_SHA", "8793c0b17963")
    report._commit_identity(refresh=True)
    label = report._head_label()
    assert "8793c0b17963" in label
    assert rp.TRUST_ASSERTED in label and rp.SOURCE_ENV in label
    assert "unknown" not in label


def test_report_head_degrades_without_git_or_product_sha(monkeypatch):
    from evaluation import report
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    report._commit_identity(refresh=True)
    label = report._head_label()                      # must not raise
    assert "unavailable" in label and rp.TRUST_UNKNOWN in label


# =========================================================================== #
# report.py defect 1 — de-duplication must not conflate provenances
# =========================================================================== #
GIT_CLEAN_DOC = {"git_commit": "8793c0b", "git_dirty": False,
                 "git_commit_source": rp.SOURCE_GIT, "git_dirty_source": rp.SOURCE_GIT,
                 "commit_trust": rp.TRUST_CLEAN, "identity_warnings": [],
                 "self_identifying": True}
ENV_ASSERTED_DOC = {"git_commit": "8793c0b", "git_dirty": None,
                    "git_commit_source": rp.SOURCE_ENV,
                    "git_dirty_source": rp.SOURCE_UNAVAILABLE,
                    "commit_trust": rp.TRUST_ASSERTED,
                    "identity_warnings": ["OPERATOR-ASSERTED COMMIT: ..."],
                    "self_identifying": True}
# Shaped like a real retained summary.json, e.g. the 8793c0b round of record: the
# provenance fields do not exist at all and the commit is null.
LEGACY_NULL_DOC = {"framework": "benchmark", "arch": "fc_loop", "n_runs": 98,
                   "timestamp": "2026-07-25T14:02:21", "git_commit": None,
                   "git_dirty": None}


def test_two_packages_that_disagree_about_provenance_are_not_merged(monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR: ``sorted({d.get("git_commit") ...})`` keyed on the raw
    SHA, so a commit git READ OFF THE TREE and the same SHA an operator merely ASSERTED
    collapsed into one row printed as if the whole round were verified."""
    from evaluation import report
    groups = report._group_by_identity([("a/summary.json", GIT_CLEAN_DOC),
                                        ("b/summary.json", ENV_ASSERTED_DOC)])
    assert len(groups) == 2, (
        "a git-verified commit and an operator-asserted commit with the same SHA were "
        "merged into one row — different evidence, same key")
    trusts = {g["identity"]["commit_trust"] for g in groups}
    assert trusts == {rp.TRUST_CLEAN, rp.TRUST_ASSERTED}
    assert {f for g in groups for f in g["files"]} == {"a/summary.json", "b/summary.json"}


def test_identical_provenance_still_de_duplicates_into_one_row():
    from evaluation import report
    groups = report._group_by_identity([("a/summary.json", GIT_CLEAN_DOC),
                                        ("b/summary.json", dict(GIT_CLEAN_DOC))])
    assert len(groups) == 1
    assert sorted(groups[0]["files"]) == ["a/summary.json", "b/summary.json"]


def test_a_file_with_no_commit_gets_its_own_row_instead_of_vanishing():
    """The old filter was ``if d and d.get("git_commit")``, so a package that could not
    name its commit was silently dropped from the section that exists to say which commit
    produced the numbers."""
    from evaluation import report
    groups = report._group_by_identity([("a/summary.json", GIT_CLEAN_DOC),
                                        ("memory_eval.json", LEGACY_NULL_DOC)])
    assert len(groups) == 2
    nulls = [g for g in groups if g["identity"]["git_commit"] is None]
    assert len(nulls) == 1 and nulls[0]["files"] == ["memory_eval.json"]
    assert nulls[0]["identity"]["git_commit_source"] == rp.SOURCE_UNRECORDED


def test_report_section_2_renders_every_provenance_and_flags_the_disagreement(
        tmp_path, monkeypatch):
    from evaluation import report
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    report._commit_identity(refresh=True)
    for name, doc in (("sweep", GIT_CLEAN_DOC), ("sweep-legacy", ENV_ASSERTED_DOC)):
        d = tmp_path / name
        d.mkdir()
        (d / "summary.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "memory_eval.json").write_text(json.dumps(LEGACY_NULL_DOC), encoding="utf-8")

    md = report.build_report_md(tmp_path, "2026-07-27T00:00:00")
    section = md.split("## 2. Git commit")[1].split("## 3.")[0]
    assert rp.TRUST_CLEAN in section and rp.TRUST_ASSERTED in section
    assert rp.SOURCE_UNRECORDED in section          # the legacy null package is shown
    assert "(no commit recorded)" in section
    assert "do not share one identity" in section
    assert "tree unknown" in section, "an unread tree must not render as 'clean'"


def _router_line(md):
    section = md.split("## 4. Models + versions")[1].split("## 5.")[0]
    return next(ln for ln in section.splitlines() if ln.startswith("- Router maps"))


def test_report_never_names_a_retired_model_as_the_one_in_use(tmp_path, monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR. The line read "light nodes to `deepseek-chat` and
    strong/thinking nodes to `deepseek-reasoner`" — the first hardcoded, the second a
    ``.get`` default. The provider RETIRED BOTH on 2026-07-24, so a report generated
    without an ablation file stated two dead models as fact."""
    from evaluation import report
    from uk_rent_agent.llm.router import RETIRED_MODEL_NAMES
    no_git(monkeypatch)
    report._commit_identity(refresh=True)
    line = _router_line(report.build_report_md(tmp_path, "2026-07-27T00:00:00"))
    assert line.count("not recorded") == 2, line
    for dead in RETIRED_MODEL_NAMES:
        assert f"`{dead}`" not in line, f"report names the RETIRED model {dead} as in use"


def test_report_names_the_models_the_ablation_actually_observed(tmp_path, monkeypatch):
    """The fix is not "always say unrecorded": a recorded observation is printed verbatim."""
    from evaluation import report
    no_git(monkeypatch)
    report._commit_identity(refresh=True)
    (tmp_path / "ablation_model.json").write_text(json.dumps({
        "chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro",
        "git_commit": None}), encoding="utf-8")
    line = _router_line(report.build_report_md(tmp_path, "2026-07-27T00:00:00"))
    assert "`deepseek-v4-flash`" in line and "`deepseek-v4-pro`" in line
    assert "not recorded" not in line


# =========================================================================== #
# run_ablation defect 2 — the retired-model guard must not be swallowed
# =========================================================================== #
def test_the_retired_model_guard_is_not_swallowed(monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR: ``except Exception: return "deepseek-reasoner"`` ate the
    RetiredModelError that ``ModelRouter()`` raises, and then returned the very name the
    guard had just refused. An ablation run under a stale env would label its whole report
    with a retired model and report it as observed — the same swallow shape as the observer
    wiring in app/app.py."""
    from evaluation import run_ablation
    from uk_rent_agent.llm.router import RetiredModelError
    monkeypatch.setenv("DEEPSEEK_REASONER_MODEL", "deepseek-reasoner")
    with pytest.raises(RetiredModelError):
        run_ablation._reasoner_model_name()


def test_an_unreachable_router_yields_no_model_name_rather_than_a_guess(monkeypatch):
    """The old fallback was itself a retired name, so even an unrelated failure produced a
    confident, wrong, DEAD label. Unresolved must stay unresolved."""
    from evaluation import run_ablation
    from uk_rent_agent.llm import router as router_mod

    class Boom:
        def __init__(self):
            raise RuntimeError("no router in this environment")

    monkeypatch.setattr(router_mod, "ModelRouter", Boom)
    assert run_ablation._reasoner_model_name() is None


def test_an_unresolved_model_is_labelled_unresolved_in_the_report(tmp_path, monkeypatch):
    from evaluation import run_ablation
    from uk_rent_agent.llm.router import RETIRED_MODEL_NAMES
    no_git(monkeypatch)
    monkeypatch.setattr(run_ablation, "_router_models",
                        lambda: {"chat_model": None, "reasoner_model": None})
    result = _run_model_study(tmp_path, monkeypatch)
    assert result["reasoner_model_resolved"] is False
    assert result["chat_model_resolved"] is False
    for key in ("reasoner_model", "chat_model"):
        assert "UNRESOLVED" in result[key]
        assert result[key] not in RETIRED_MODEL_NAMES


def test_a_resolved_model_is_reported_verbatim(tmp_path, monkeypatch):
    from evaluation import run_ablation
    no_git(monkeypatch)
    monkeypatch.setattr(run_ablation, "_router_models",
                        lambda: {"chat_model": "deepseek-v4-flash",
                                 "reasoner_model": "deepseek-v4-pro"})
    result = _run_model_study(tmp_path, monkeypatch)
    assert result["reasoner_model"] == "deepseek-v4-pro"
    assert result["chat_model"] == "deepseek-v4-flash"
    assert result["reasoner_model_resolved"] is True and result["chat_model_resolved"] is True


def test_the_ablation_reports_the_live_routers_real_names(monkeypatch):
    """End-to-end against the REAL router (construction makes no network call): the names
    the ablation would label a report with must be the live ones, not retired aliases."""
    from evaluation import run_ablation
    from uk_rent_agent.llm.router import RETIRED_MODEL_NAMES
    for var in ("DEEPSEEK_MODEL", "DEEPSEEK_CHAT_MODEL", "DEEPSEEK_REASONER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    models = run_ablation._router_models()
    assert models["chat_model"] and models["reasoner_model"]
    assert not (RETIRED_MODEL_NAMES & set(models.values()))


def test_is_strong_tolerates_an_unresolved_reasoner_name():
    """``_is_strong`` used to do ``reasoner.lower()`` unconditionally; None must not crash
    the aggregation that reports the unresolved state."""
    from evaluation.run_ablation import _is_strong
    assert _is_strong("deepseek-reasoner", None) is True
    assert _is_strong("deepseek-v4-flash", None) is False
    assert _is_strong("deepseek-v4-flash", "deepseek-v4-flash") is True
    assert _is_strong(None, None) is False


# =========================================================================== #
# BACKWARD COMPATIBILITY — every retained package on disk has git_commit: null
# =========================================================================== #
def test_a_legacy_null_package_still_reads_and_is_never_upgraded():
    """The 8793c0b round of record IS the evidence base. It predates provenance entirely,
    so it must keep reading — and must be reported as ``unrecorded``, which is a DIFFERENT
    claim from a new package that recorded ``unavailable``."""
    ident = rp.describe_identity(LEGACY_NULL_DOC)          # must not raise
    assert ident["git_commit"] is None
    assert ident["git_commit_source"] == rp.SOURCE_UNRECORDED
    assert ident["commit_trust"] == rp.TRUST_UNKNOWN
    assert ident["self_identifying"] is False
    assert any("legacy package" in w for w in ident["identity_warnings"])


def test_the_report_renders_a_whole_legacy_round_without_crashing(tmp_path, monkeypatch):
    """A results dir containing ONLY pre-provenance artifacts — exactly what is on disk
    today — must still produce a report."""
    from evaluation import report
    no_git(monkeypatch)
    report._commit_identity(refresh=True)
    for sub in ("sweep", "sweep-legacy"):
        d = tmp_path / sub
        d.mkdir()
        (d / "summary.json").write_text(json.dumps(LEGACY_NULL_DOC), encoding="utf-8")
    for name in ("ablation_model.json", "ablation_retrieval.json", "fault_summary.json",
                 "memory_eval.json"):
        (tmp_path / name).write_text(
            json.dumps({"git_commit": None, "timestamp": "2026-07-25T14:02:21"}),
            encoding="utf-8")
    md = report.build_report_md(tmp_path, "2026-07-27T00:00:00")
    assert "## 2. Git commit" in md
    assert rp.SOURCE_UNRECORDED in md
    # One shared (null, unrecorded) identity across all six files => ONE row, not six.
    section = md.split("## 2. Git commit")[1].split("## 3.")[0]
    assert section.count("_(no commit recorded)_") == 1
    assert "do not share one identity" not in section


def test_cv_metrics_renders_for_a_legacy_round(tmp_path, monkeypatch):
    """CV_METRICS.md takes its HEAD from the same cached identity; a legacy round must not
    break it either."""
    from evaluation import report
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    report._commit_identity(refresh=True)
    d = tmp_path / "sweep"
    d.mkdir()
    (d / "summary.json").write_text(json.dumps(LEGACY_NULL_DOC), encoding="utf-8")
    cv = report.build_cv_md(tmp_path, "2026-07-27T00:00:00")
    assert rp.TRUST_UNKNOWN in cv and "unavailable" in cv


@pytest.mark.parametrize("round_dir", [
    Path("/home/shuhan/uk_rent_recommendation/.runtime/"
         "round-8793c0b-internal-2026-07-25/eval/sweep"),
])
def test_the_real_round_of_record_still_reads_when_it_is_present(round_dir):
    """Reads the RETAINED package itself when this checkout can see it (it lives outside
    the repo, so the containerised suite skips). Read-only."""
    summary = round_dir / "summary.json"
    if not summary.exists():
        pytest.skip(f"retained round not mounted here: {round_dir}")
    for name in ("summary.json", "manifest.json"):
        doc = json.loads((round_dir / name).read_text(encoding="utf-8"))
        ident = rp.describe_identity(doc)
        assert ident["git_commit"] is None
        assert ident["git_commit_source"] == rp.SOURCE_UNRECORDED
        assert ident["commit_trust"] == rp.TRUST_UNKNOWN
    # The re-score identity gate reads product_sha / case_contract_sha256 off the manifest,
    # which this change does not touch: the round stays re-scorable.
    manifest = json.loads((round_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["product_sha"] and manifest["case_contract_sha256"]


# =========================================================================== #
# SOURCE GUARD — a promise in a docstring is not a guard
# =========================================================================== #
# results_package.py IS the probe's home. rescore.py is NOT owned by this change and still
# runs its own git probe for ``evaluator_sha``; it is exempted HERE, deliberately and
# visibly, so the exemption is a decision on the record rather than a silent hole.
GIT_PROBE_HOME = {"results_package.py"}
GIT_PROBE_EXEMPT = {"rescore.py"}


def _non_docstring_str_constants(tree):
    doc_ids = {id(n.value) for n in ast.walk(tree)
               if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in doc_ids]


def test_no_writer_builds_its_own_git_probe():
    """Every commit answer in ``evaluation/`` comes from results_package, or the writers
    drift apart again — which is exactly how five of them ended up disagreeing about what
    "no git" means."""
    offenders = []
    for path in sorted((REPO_ROOT / "evaluation").rglob("*.py")):
        if path.name in GIT_PROBE_HOME | GIT_PROBE_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in _non_docstring_str_constants(tree):
            v = node.value
            if v == "git" or "rev-parse" in v or "--porcelain" in v:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {v!r}")
    assert not offenders, (
        "these files invoke git directly instead of using "
        "results_package.resolve_commit_identity:\n  " + "\n  ".join(offenders))


@pytest.mark.parametrize("mod_name", WRITER_MODULES)
def test_every_writer_resolves_identity_through_results_package(mod_name):
    mod = importlib.import_module(mod_name)
    assert hasattr(mod, "_commit_identity"), (
        f"{mod_name} has no _commit_identity — it cannot state which commit produced it")
    assert not hasattr(mod, "_git_commit"), (
        f"{mod_name} still carries the old git-only _git_commit probe")


@pytest.mark.parametrize("mod_name", WRITER_MODULES)
def test_every_writer_degrades_instead_of_crashing_when_git_is_absent(mod_name, monkeypatch):
    """"No git available" is the NORMAL container condition, not an edge case."""
    no_git(monkeypatch)
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    mod = importlib.import_module(mod_name)
    ident = (mod._commit_identity(refresh=True) if mod_name == "evaluation.report"
             else mod._commit_identity())
    assert_full_identity(ident, mod_name)
    assert ident["git_commit"] is None and ident["git_dirty"] is None
    assert ident["commit_trust"] == rp.TRUST_UNKNOWN


@pytest.mark.parametrize("mod_name", WRITER_MODULES)
def test_no_writer_promotes_an_asserted_sha_into_a_dirty_claim(mod_name, monkeypatch):
    """Only git can witness a working tree. An asserted SHA must leave dirtiness UNKNOWN —
    the invariant results_package.assert_identity_consistent enforces, checked at each of
    the five call sites so a future writer cannot hand-roll around it."""
    no_git(monkeypatch)
    monkeypatch.setenv("PRODUCT_SHA", "8793c0b17963")
    mod = importlib.import_module(mod_name)
    ident = (mod._commit_identity(refresh=True) if mod_name == "evaluation.report"
             else mod._commit_identity())
    assert ident["git_commit"] == "8793c0b17963"
    assert ident["git_dirty"] is None
    assert ident["git_dirty_source"] == rp.SOURCE_UNAVAILABLE
    assert ident["commit_trust"] == rp.TRUST_ASSERTED
