"""`scripts/canary_prune_fixture_rows.py` removes fixture rows and nothing else.

The live legacy log carries 121 rows no pool ever wrote (fixture shas `shaLEGACY`
/ `shaSER`, including 32 `canary.arch_mismatch` events with no ts, request_id or
schema version). The danger of any such script is that it becomes a log cleaner,
so these tests pin what it must REFUSE to delete as hard as what it deletes.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "canary_prune_f5b", _ROOT / "scripts" / "canary_prune_fixture_rows.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["canary_prune_f5b"] = mod
    spec.loader.exec_module(mod)
    return mod


cp = _load()

GENUINE = {"event": "canary.turn", "ts": "2026-09-03T10:00:00Z",
           "request_id": "r1", "telemetry_schema_version": 3,
           "candidate_sha": "1df0d4f7140f366e42b120fc9629fdb87e738428"}
SHORT_SHA = {**GENUINE, "request_id": "r2", "candidate_sha": "4171d84",
             "telemetry_schema_version": 2}
UNKNOWN_SHA = {**GENUINE, "request_id": "r3", "candidate_sha": "unknown",
               "telemetry_schema_version": 2}
FIXTURE_TURN = {"event": "canary.turn", "ts": "2026-08-31T11:40:13Z",
                "request_id": "r4", "telemetry_schema_version": 2,
                "candidate_sha": "shaLEGACY"}
FIXTURE_SER = {**FIXTURE_TURN, "request_id": "r5", "candidate_sha": "shaSER"}
ARCH_MISMATCH = {"event": "canary.arch_mismatch", "conversation_id": "c1",
                 "stored_arch": "fc_loop", "serving_arch": "legacy",
                 "candidate_sha": "shaLEGACY"}
IDENTITYLESS = {"event": "canary.arch_mismatch", "conversation_id": "c2",
                "stored_arch": "fc_loop", "serving_arch": "legacy"}


def _write(tmp_path, rows, *, extra_lines=()):
    path = tmp_path / "canary-legacy.jsonl"
    lines = [json.dumps(row) for row in rows] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_dry_run_is_the_default_and_writes_nothing(tmp_path):
    path = _write(tmp_path, [GENUINE, FIXTURE_TURN])
    before = path.read_text(encoding="utf-8")
    out = io.StringIO()

    rc = cp.prune(str(path), apply=False, backup_dir=None, out=out)

    assert rc == cp.EXIT_OK
    assert path.read_text(encoding="utf-8") == before
    assert "DRY RUN" in out.getvalue()
    assert "rows to remove  : 1" in out.getvalue()
    assert list(tmp_path.iterdir()) == [path]


def test_drops_only_fixture_shas_and_identityless_events(tmp_path):
    rows = [GENUINE, SHORT_SHA, UNKNOWN_SHA, FIXTURE_TURN, FIXTURE_SER,
            ARCH_MISMATCH, IDENTITYLESS]
    path = _write(tmp_path, rows)
    out = io.StringIO()

    rc = cp.prune(str(path), apply=True, backup_dir=str(tmp_path / "bak"), out=out)

    assert rc == cp.EXIT_OK
    kept = [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert kept == [GENUINE, SHORT_SHA, UNKNOWN_SHA]
    assert "rows to remove  : 4" in out.getvalue()


def test_genuine_short_and_unknown_sha_rows_are_never_touched(tmp_path):
    """1560 short-sha and 1222 unknown-sha rows are real production turns.

    They are also where every zero-tolerance counter in the file lives, so a
    script that deleted them would change a gate verdict by deleting evidence.
    """
    path = _write(tmp_path, [SHORT_SHA, UNKNOWN_SHA])
    out = io.StringIO()

    rc = cp.prune(str(path), apply=True, backup_dir=str(tmp_path / "bak"), out=out)

    assert rc == cp.EXIT_OK
    assert "nothing to do" in out.getvalue()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_unparseable_lines_are_kept_not_silently_dropped(tmp_path):
    path = _write(tmp_path, [GENUINE, FIXTURE_TURN],
                  extra_lines=['{"event": "canary.turn", "ts": "2026-09-0'])
    out = io.StringIO()

    cp.prune(str(path), apply=True, backup_dir=str(tmp_path / "bak"), out=out)

    text = path.read_text(encoding="utf-8")
    assert '{"event": "canary.turn", "ts": "2026-09-0' in text
    assert "shaLEGACY" not in text


def test_a_backup_is_written_before_the_rewrite(tmp_path):
    path = _write(tmp_path, [GENUINE, FIXTURE_TURN])
    original = path.read_text(encoding="utf-8")
    backups = tmp_path / "bak"

    cp.prune(str(path), apply=True, backup_dir=str(backups), out=io.StringIO())

    written = list(backups.iterdir())
    assert len(written) == 1
    assert written[0].name.startswith("canary-legacy.jsonl.bak-")
    # NOT a .jsonl suffix: canary_report.LOG_SUFFIXES would otherwise read the
    # backup on a directory walk and double the record population.
    assert not written[0].name.endswith(".jsonl")
    assert written[0].read_text(encoding="utf-8") == original


def test_an_existing_backup_is_never_overwritten(tmp_path):
    path = _write(tmp_path, [GENUINE, FIXTURE_TURN])
    backups = tmp_path / "bak"
    cp.prune(str(path), apply=True, backup_dir=str(backups), out=io.StringIO())
    path.write_text(path.read_text(encoding="utf-8")
                    + json.dumps(FIXTURE_SER) + "\n", encoding="utf-8")
    out = io.StringIO()

    rc = cp.prune(str(path), apply=True, backup_dir=str(backups), out=out)

    assert rc == cp.EXIT_ERROR
    assert "backup already exists" in out.getvalue()


def test_a_file_that_changes_during_the_run_aborts_without_writing(tmp_path,
                                                                   monkeypatch):
    path = _write(tmp_path, [GENUINE, FIXTURE_TURN])
    before = path.read_text(encoding="utf-8")
    real = cp._stat_signature
    calls = {"n": 0}

    def _drifting(p):
        calls["n"] += 1
        signature = real(p)
        # First call is the "before" snapshot; every later call reports a changed
        # file, i.e. a pool still appending to the live log.
        return signature if calls["n"] == 1 else (signature[0], signature[1] + 1,
                                                  signature[2] + 1)

    monkeypatch.setattr(cp, "_stat_signature", _drifting)
    out = io.StringIO()

    rc = cp.prune(str(path), apply=True, backup_dir=str(tmp_path / "bak"), out=out)

    assert rc == cp.EXIT_RACE
    assert path.read_text(encoding="utf-8") == before
    assert "still writing" in out.getvalue()


def test_classify_line_reasons():
    assert cp.classify_line(json.dumps(GENUINE))[0] is False
    assert cp.classify_line(json.dumps(FIXTURE_TURN))[0] is True
    assert cp.classify_line(json.dumps(IDENTITYLESS))[0] is True
    assert cp.classify_line("")[0] is False
    assert cp.classify_line("not json")[0] is False
    assert cp.classify_line("[1, 2, 3]")[0] is False
