"""Measurement infrastructure: identity, evidence persistence, preflight, reuse guard.

Landed as PURE evaluation infrastructure (2026-07-23 ruling) after two candidate branches
were terminated. It carries no product, case-contract or critic/filter behaviour change —
only the machinery that makes a paired A/B trustworthy:

  * three-layer identity (product / capture / evaluator) so a measurement probe can never
    masquerade as the product under test;
  * per-run grader-input persistence, so any arm can be re-scored later;
  * single-evaluator re-scoring with identity refusal, evidence-digest verification and
    run_id de-duplication;
  * a preflight that validates EVERY benchmark shard, not just the one being run;
  * refusing a non-empty output dir by default.

Each check below exists because the corresponding failure actually happened.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation import results_package as rp
from evaluation import run_benchmark as rb


# ── three-layer identity ─────────────────────────────────────────────
def _manifest(tmp_path, **env):
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "A1"}\n', encoding="utf-8")
    events = tmp_path / "events.jsonl"
    events.write_text('{"type": "node_span"}\n', encoding="utf-8")
    return rp.build_manifest(
        argv=["python", "-m", "evaluation.run_benchmark"], arch="fc_loop",
        config="routed_models", timestamp="2026-07-23T00:00:00",
        case_file=case_file, events_log=events, mode="live",
        git_commit=lambda: "capture1", git_dirty=lambda: False)


def test_capture_tree_does_not_masquerade_as_the_product(tmp_path, monkeypatch):
    """The whole point: a probe commit on top of a baseline must report the BASELINE as
    the product and itself as the capture tree."""
    monkeypatch.setenv("PRODUCT_SHA", "baseline0")
    m = _manifest(tmp_path)
    assert m["product_sha"] == "baseline0"
    assert m["capture_sha"] == "capture1"
    assert m["capture_is_product"] is False


def test_product_defaults_to_the_running_tree_when_not_pinned(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    m = _manifest(tmp_path)
    assert m["product_sha"] == "capture1" and m["capture_sha"] == "capture1"
    assert m["capture_is_product"] is True


def test_evaluator_is_null_until_a_rescore_stamps_it(tmp_path, monkeypatch):
    """A tree's own verdicts are not the gate, so it must not claim to be the evaluator."""
    monkeypatch.delenv("EVALUATOR_SHA", raising=False)
    assert _manifest(tmp_path)["evaluator_sha"] is None


def test_manifest_pins_grader_and_case_contract_digests(tmp_path):
    m = _manifest(tmp_path)
    assert m["grader_sha256"] and m["case_contract_sha256"]


# ── commit binding: a summary must be able to name its own commit ────
#
# The defect (fixed 2026-07-26): the harness probed git ONLY, and it runs inside a
# container off a bind mount with no git dir — so it recorded ``git_commit: null`` and
# ``git_dirty: null`` even when PRODUCT_SHA was pinned. Every summary's binding to a
# commit therefore lived OUTSIDE the package, maintained by hand, one file move away from
# being permanently unattributable. This project's entire method is "a measurement belongs
# to exactly one SHA", so the fix records the commit AND who vouches for it.
def _no_git():
    """The container case: git cannot answer at all."""
    return (None, None)


def test_commit_and_dirty_come_from_git_when_git_can_answer():
    ident = rp.resolve_commit_identity(git_probe=lambda: ("abc1234", False), env={})
    assert ident["git_commit"] == "abc1234"
    assert ident["git_dirty"] is False
    assert ident["git_commit_source"] == rp.SOURCE_GIT
    assert ident["git_dirty_source"] == rp.SOURCE_GIT
    assert ident["commit_trust"] == rp.TRUST_CLEAN
    assert ident["self_identifying"] is True
    assert ident["identity_warnings"] == []          # the ONLY quiet outcome


def test_product_sha_is_the_fallback_and_is_labelled_as_asserted():
    """FAILS ON THE OLD BEHAVIOUR (old code recorded None here). The fallback must fire —
    and must not pass an operator's assertion off as a reading from the tree."""
    ident = rp.resolve_commit_identity(git_probe=_no_git,
                                       env={"PRODUCT_SHA": "8793c0b17963"})
    assert ident["git_commit"] == "8793c0b17963"
    assert ident["self_identifying"] is True
    assert ident["git_commit_source"] == "env:PRODUCT_SHA" == rp.SOURCE_ENV
    assert ident["commit_trust"] == rp.TRUST_ASSERTED
    # Dirtiness is UNKNOWABLE without git: it must not be guessed as clean.
    assert ident["git_dirty"] is None
    assert ident["git_dirty_source"] == rp.SOURCE_UNAVAILABLE
    assert any("PRODUCT_SHA" in w and "UNKNOWN" in w for w in ident["identity_warnings"])


def test_no_git_and_no_product_sha_says_so_instead_of_pretending():
    ident = rp.resolve_commit_identity(git_probe=_no_git, env={})
    assert ident["git_commit"] is None and ident["self_identifying"] is False
    assert ident["git_commit_source"] == rp.SOURCE_UNAVAILABLE
    assert ident["commit_trust"] == rp.TRUST_UNKNOWN
    assert any("NO COMMIT BINDING" in w for w in ident["identity_warnings"])


def test_a_dirty_tree_is_recorded_dirty_and_loudly():
    """Rule 5 is 'build only from clean checkouts', so a measurement taken on a dirty tree
    must be self-evidently suspect to whoever reads the package later."""
    ident = rp.resolve_commit_identity(git_probe=lambda: ("abc1234", True), env={})
    assert ident["git_dirty"] is True
    assert ident["commit_trust"] == rp.TRUST_DIRTY == "GIT-DIRTY"
    assert any("DIRTY TREE" in w for w in ident["identity_warnings"])
    assert any("rule 5" in w.lower() for w in ident["identity_warnings"])


def test_git_binary_absent_degrades_to_the_env_var_instead_of_crashing(monkeypatch):
    """The harness runs in a container: no git binary, no git dir. Probing must degrade,
    never raise — a round must not die because it could not name itself."""
    def boom():
        raise FileNotFoundError("git")
    ident = rp.resolve_commit_identity(git_probe=boom, env={"PRODUCT_SHA": "deadbee"})
    assert ident["git_commit"] == "deadbee"
    assert ident["git_commit_source"] == rp.SOURCE_ENV


def test_probe_git_returns_none_pair_outside_a_repository(tmp_path, monkeypatch):
    """Exercises the REAL probe (no stub) somewhere that is not a git repo, and with an
    empty PATH so the binary itself is missing."""
    assert rp.probe_git(tmp_path) == (None, None)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert rp.probe_git(tmp_path) == (None, None)
    # The run_benchmark-level probes stay GIT-ONLY (no PRODUCT_SHA fallback): capture_sha
    # is a claim about executed code, and only git can witness that.
    monkeypatch.setattr(rb, "REPO_ROOT", tmp_path)
    assert rb._git_commit() is None and rb._git_dirty() is None


def test_dirty_probe_distinguishes_a_clean_tree_from_an_unanswerable_one():
    """``git status --porcelain`` returning "" means CLEAN; it must not be confused with
    git failing to answer, which means UNKNOWN."""
    clean = rp.resolve_commit_identity(git_probe=lambda: ("c0ffee1", False), env={})
    unknown = rp.resolve_commit_identity(git_probe=lambda: ("c0ffee1", None), env={})
    assert clean["git_dirty"] is False and clean["commit_trust"] == rp.TRUST_CLEAN
    assert unknown["git_dirty"] is None
    assert unknown["commit_trust"] == rp.TRUST_UNKNOWN
    assert any("UNKNOWN" in w for w in unknown["identity_warnings"])


# ── the source guard (not a promise): refuse to publish a false identity ──
def test_guard_refuses_a_dirty_tree_that_is_not_shouted_about():
    with pytest.raises(ValueError, match="LOUDLY"):
        rp.assert_identity_consistent(
            {"git_commit": "abc1234", "git_dirty": True,
             "git_commit_source": rp.SOURCE_GIT, "commit_trust": rp.TRUST_DIRTY,
             "identity_warnings": ["tree had changes"]})


def test_guard_refuses_a_dirty_claim_on_an_asserted_commit():
    """Only git can witness the working tree; PRODUCT_SHA cannot vouch for cleanliness."""
    with pytest.raises(ValueError, match="only git can observe"):
        rp.assert_identity_consistent(
            {"git_commit": "abc1234", "git_dirty": False,
             "git_commit_source": rp.SOURCE_ENV, "commit_trust": rp.TRUST_ASSERTED,
             "identity_warnings": ["asserted"]})


def test_guard_refuses_a_commit_whose_provenance_disagrees_with_it():
    with pytest.raises(ValueError, match="provenance disagrees"):
        rp.assert_identity_consistent(
            {"git_commit": None, "git_dirty": None, "git_commit_source": rp.SOURCE_GIT,
             "commit_trust": rp.TRUST_UNKNOWN, "identity_warnings": []})
    with pytest.raises(ValueError, match="provenance disagrees"):
        rp.assert_identity_consistent(
            {"git_commit": "abc1234", "git_dirty": None,
             "git_commit_source": rp.SOURCE_UNAVAILABLE,
             "commit_trust": rp.TRUST_UNKNOWN, "identity_warnings": []})


def test_guard_refuses_a_clean_claim_that_git_never_backed():
    with pytest.raises(ValueError, match="git-clean"):
        rp.assert_identity_consistent(
            {"git_commit": "abc1234", "git_dirty": None,
             "git_commit_source": rp.SOURCE_ENV, "commit_trust": rp.TRUST_CLEAN,
             "identity_warnings": ["asserted"]})


def test_guard_refuses_an_unknown_provenance_label():
    with pytest.raises(ValueError, match="is not one of"):
        rp.assert_identity_consistent(
            {"git_commit": "abc1234", "git_dirty": False,
             "git_commit_source": "vibes", "commit_trust": rp.TRUST_CLEAN,
             "identity_warnings": []})


# ── the binding reaches the artifacts a reader actually opens ─────────
def test_manifest_records_the_commit_and_its_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("PRODUCT_SHA", raising=False)
    m = _manifest(tmp_path)
    assert m["git_commit"] == "capture1"
    assert m["git_commit_source"] == rp.SOURCE_GIT
    assert m["commit_trust"] == rp.TRUST_CLEAN
    assert m["identity_warnings"] == [] and m["self_identifying"] is True


def test_manifest_records_the_env_fallback_when_the_container_has_no_git(tmp_path, monkeypatch):
    """FAILS ON THE OLD BEHAVIOUR: the manifest used to write git_commit: null here, which
    is exactly what every retained round on disk shows."""
    monkeypatch.setenv("PRODUCT_SHA", "8793c0b17963")
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "A1"}\n', encoding="utf-8")
    m = rp.build_manifest(
        argv=["x"], arch="fc_loop", config="routed_models", timestamp="t",
        case_file=case_file, events_log=case_file, mode="live",
        git_commit=lambda: None, git_dirty=lambda: None)
    assert m["git_commit"] == "8793c0b17963"
    assert m["git_commit_source"] == rp.SOURCE_ENV
    assert m["commit_trust"] == rp.TRUST_ASSERTED
    assert m["self_identifying"] is True


def test_an_asserted_sha_never_becomes_the_capture_tree(tmp_path, monkeypatch):
    """The commit-binding fallback must NOT weaken the three-layer identity: capture_sha is
    a claim about code that RAN, so it stays git-only. Promoting PRODUCT_SHA into it would
    make capture_sha == product_sha and flip capture_is_product to True — the exact
    masquerade the three layers exist to prevent."""
    monkeypatch.setenv("PRODUCT_SHA", "baseline0")
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "A1"}\n', encoding="utf-8")
    m = rp.build_manifest(
        argv=["x"], arch="fc_loop", config="routed_models", timestamp="t",
        case_file=case_file, events_log=case_file, mode="live",
        git_commit=lambda: None, git_dirty=lambda: None)
    assert m["git_commit"] == "baseline0"        # the binding is recorded …
    assert m["capture_sha"] is None              # … but the capture tree is NOT claimed
    assert m["capture_is_product"] is False
    assert m["product_sha"] == "baseline0"       # unchanged: still rescorable


def test_summary_and_manifest_of_one_run_cannot_name_different_commits(tmp_path, monkeypatch):
    """One resolution per process. A summary and a manifest disagreeing about the commit
    would be worse than a null: two SHAs for one measurement."""
    monkeypatch.setattr(rb, "_IDENTITY_CACHE", {}, raising=False)
    monkeypatch.setattr(rp, "probe_git", lambda *_a, **_k: ("f00d123", True))
    ident = rb.commit_identity(refresh=True)
    summary = rb.write_summary(tmp_path, [], mode="offline", cfg_name="c", repeats=1,
                               cost_cap=0.0, stopped_reason=None, n_selected=0,
                               timestamp="t", arch="fc_loop", identity=ident)
    case_file = tmp_path / "cases.jsonl"
    case_file.write_text('{"case_id": "A1"}\n', encoding="utf-8")
    m = rp.build_manifest(argv=["x"], arch="fc_loop", config="c", timestamp="t",
                          case_file=case_file, events_log=case_file, mode="offline",
                          identity=ident)
    for key in ("git_commit", "git_dirty", "git_commit_source", "commit_trust"):
        assert summary[key] == m[key], key
    on_disk = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert on_disk["git_commit"] == "f00d123"
    assert on_disk["git_dirty"] is True
    assert on_disk["commit_trust"] == rp.TRUST_DIRTY
    assert any("DIRTY TREE" in w for w in on_disk["identity_warnings"])


def test_the_dirty_warning_is_shouted_before_the_round_spends_anything(capsys):
    ident = rp.resolve_commit_identity(git_probe=lambda: ("abc1234", True), env={})
    rb.announce_identity(ident)
    out = capsys.readouterr().out
    assert "trust=GIT-DIRTY" in out and "DIRTY TREE" in out


# ── backward compatibility: every retained package has git_commit: null ──
# `.runtime/round-8793c0b-internal-2026-07-25/` is THE ROUND OF RECORD and must stay
# readable and re-scorable. Adding identity must never make reading an old package raise.
def _round_of_record_manifest():
    """Shaped like the real 8793c0b manifest.json: PRODUCT_SHA was pinned, git was
    unavailable in the container, so git_commit/git_dirty/capture_sha are all null and the
    provenance fields do not exist at all."""
    return {"argv": ["python", "-m", "evaluation.run_benchmark"], "mode": "live",
            "product_sha": "8793c0b17963a6a2b375903a164d3d96395dc834",
            "capture_sha": None, "evaluator_sha": None, "capture_is_product": False,
            "case_contract_sha256": "7f1ead524c421e33f4098afff036f019a92537d5f1f76deba5",
            "timestamp": "2026-07-25T14:02:21", "git_commit": None, "git_dirty": None}


def test_an_old_package_with_a_null_commit_still_reads(tmp_path):
    old = _round_of_record_manifest()
    ident = rp.describe_identity(old)                     # must not raise
    assert ident["git_commit"] is None
    assert ident["self_identifying"] is False
    assert ident["git_commit_source"] == rp.SOURCE_UNRECORDED
    assert ident["commit_trust"] == rp.TRUST_UNKNOWN
    assert any("legacy package" in w for w in ident["identity_warnings"])
    # …and reading it off disk, as report.py does, is likewise fine.
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(old), encoding="utf-8")
    assert rp.describe_identity(json.loads(p.read_text()))["git_commit"] is None


def test_describe_identity_never_raises_on_any_shape():
    for doc in (None, {}, {"git_commit": "7db03e7", "git_dirty": False},
                {"git_commit": "7db03e7", "git_dirty": True},
                {"git_commit": None}, {"git_dirty": None}, "not a dict", 7):
        assert "commit_trust" in rp.describe_identity(doc)
    # A legacy package that recorded a commit is NOT relabelled as if git had vouched.
    legacy = rp.describe_identity({"git_commit": "7db03e7", "git_dirty": False})
    assert legacy["git_commit_source"] == rp.SOURCE_UNRECORDED
    assert legacy["commit_trust"] == rp.TRUST_UNRECORDED
    # A legacy DIRTY package still reads as dirty, loudly.
    dirty = rp.describe_identity({"git_commit": "7db03e7", "git_dirty": True})
    assert dirty["commit_trust"] == rp.TRUST_DIRTY
    assert any("DIRTY TREE" in w for w in dirty["identity_warnings"])


def test_describe_identity_reports_a_new_package_verbatim():
    """"unavailable" (a new writer looked and found nothing) must stay distinguishable from
    "unrecorded" (an old writer never looked)."""
    fresh = rp.resolve_commit_identity(git_probe=_no_git, env={})
    read_back = rp.describe_identity(fresh)
    assert read_back["git_commit_source"] == rp.SOURCE_UNAVAILABLE
    assert read_back["commit_trust"] == rp.TRUST_UNKNOWN


def test_the_round_of_record_is_still_rescorable_after_the_change(tmp_path):
    """The identity REFUSAL in rescore.py is untouched: the 8793c0b manifest shape (null
    git_commit, null capture_sha, product_sha pinned) still passes the gate exactly as
    before, because capture falls back to product_sha."""
    from evaluation import rescore
    d = _run_dir(tmp_path, "round_of_record", _round_of_record_manifest(), [_rec()])
    r = rescore.rescore_dir(d, {}, None)
    assert r["rescorable"] is True
    assert r["product_sha"] == "8793c0b17963a6a2b375903a164d3d96395dc834"
    assert r["capture_sha"] == r["product_sha"]


# ── preflight: EVERY shard, not just the one being run ───────────────
def test_preflight_validates_every_shard_in_the_repo():
    """The failure this prevents: a constraint added to cases.jsonl but not to
    schema.json survived two green guard runs, because the guard uses a different
    shard — while the Base98 contract was unloadable the whole time."""
    assert rb.validate_all_shards() == []


def test_preflight_reports_a_bad_shard(tmp_path):
    (tmp_path / "cases_broken.jsonl").write_text(
        json.dumps({"case_id": "X1", "expected_constraints": [{"type": "not_a_real_type"}]}) + "\n",
        encoding="utf-8")
    problems = rb.validate_all_shards(tmp_path)
    assert problems and "cases_broken.jsonl" in problems[0]


def test_preflight_flags_an_empty_benchmark_dir(tmp_path):
    assert rb.validate_all_shards(tmp_path) == [f"no benchmark shards found in {tmp_path}"]


# ── output-dir reuse guard ───────────────────────────────────────────
def test_fresh_or_missing_out_dir_is_allowed(tmp_path):
    rb.guard_output_dir(tmp_path / "nope")          # missing
    (tmp_path / "empty").mkdir()
    rb.guard_output_dir(tmp_path / "empty")          # present but empty


def test_non_empty_out_dir_is_refused(tmp_path):
    (tmp_path / "grader_input.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        rb.guard_output_dir(tmp_path)
    assert "non-empty output dir" in str(exc.value)


def test_reuse_can_be_opted_into_explicitly(tmp_path, capsys):
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
    rb.guard_output_dir(tmp_path, allow_reuse=True)
    assert "APPENDS" in capsys.readouterr().out


# ── the re-scorer's refusals ─────────────────────────────────────────
def _run_dir(tmp_path, name, manifest, records):
    d = tmp_path / name
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if records is not None:
        (d / "grader_input.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return d


def _rec(case_id="A1", run_id="A1#r1", passed=True):
    import hashlib
    ev = [{"tool": "search_properties", "success": True, "error": None, "data": {"x": 1}}]
    blob = json.dumps(ev, ensure_ascii=False, sort_keys=True, default=str)
    return {"run_id": run_id, "case_id": case_id, "repeat": 1,
            "raw_evidence_sha256": hashlib.sha256(blob.encode()).hexdigest(),
            "evidence": ev,
            "grader_input": {"final_answer": "ok", "tools_called": ["search_properties"],
                             "tool_call_events": [], "route": None, "user_texts": [],
                             "reference_calculations": None, "error": None,
                             "reconstructed_context": None, "history_texts": []},
            "scored_passed": passed, "scored_route_matched": True}


def test_rescore_refuses_a_run_without_persisted_evidence(tmp_path):
    from evaluation import rescore
    d = _run_dir(tmp_path, "old", {"product_sha": "p", "capture_sha": "p"}, None)
    r = rescore.rescore_dir(d, {}, None)
    assert r["rescorable"] is False and "predates evidence persistence" in r["reason"]


def test_rescore_refuses_a_missing_identity(tmp_path):
    from evaluation import rescore
    d = _run_dir(tmp_path, "noident", {"case_contract_sha256": "abc"}, [_rec()])
    r = rescore.rescore_dir(d, {}, None)
    assert r["rescorable"] is False and "IDENTITY REFUSED" in r["reason"]
    assert "product_sha" in r["reason"]


def test_rescore_refuses_a_contract_mismatch(tmp_path):
    """Two arms scored against different contracts are not a paired comparison."""
    from evaluation import rescore
    d = _run_dir(tmp_path, "mismatch",
                 {"product_sha": "p", "capture_sha": "c", "case_contract_sha256": "aaa"},
                 [_rec()])
    r = rescore.rescore_dir(d, {}, None, expected_contract="bbb")
    assert r["rescorable"] is False and "did not score the same contract" in r["reason"]
