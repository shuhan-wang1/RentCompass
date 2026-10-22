"""r6-M2: an owner-run purge of stored reflection rows that the R4-12 wording guard rejects.

5155cbd hides an escalating reflection when it is read, but the rows written before it stay in the
store. ``scripts/purge_escalating_reflections.py`` removes them: dry run by default, a JSON backup
before ``--apply`` deletes, deletion through the store's own API (tombstoned, so the legacy sync
cannot bring a row back), reflection rows only, and every reflection judged against its user's FULL
own-message history -- the verdict the read side now gives too. Built on synthetic stores only.
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "app"):
    if str(_p) in sys.path:
        sys.path.remove(str(_p))
    sys.path.insert(0, str(_p))

am_mod = importlib.import_module("rag.agent_memory")


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "purge_escalating_reflections_r6", _ROOT / "scripts" / "purge_escalating_reflections.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["purge_escalating_reflections_r6"] = mod
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()
ALICE, BOB = "user-r6-alice", "user-r6-bob"
# Verbatim recorded rows (r06/r08, see tests/test_r5_memory_reflection_wording.py).
DEAL_BREAKER = ("Beyond price and location, practical livability factors dominate: "
                "pet-friendliness (cat) and area safety are deal-breakers, not nice-to-haves.")
FIRM = ("User has a firm, non-negotiable set of core criteria: step-free access, a 1-bedroom "
        "home, located in Camden, and priced under £1,800.")
ATTRIBUTED = ("There is a conflict in the records about Canary Wharf and Manchester. The memories "
              "say the guarantor is in Canary Wharf, but the user's own request said Manchester.")
# "must" is Alice's own word, in a turn-log row that is not retrieved beside the reflection.
OWN_WORD = "A cat-friendly landlord seems to be a must for this user."
TENTATIVE = "Hackney is likely chosen for commute practicality to Canary Wharf."
ALICE_SAID_MUST = ("User asked: Please remember: my budget is at most £1,900 a month and I have a "
                   "cat called Quillbert so pets must be allowed.")


def _store(tmp_path) -> am_mod.AgentMemory:
    memory = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    memory.add(ALICE_SAID_MUST, "episodic", "s", ALICE, role="user", importance=5)
    memory.add("User asked: Find a 1-bed in Hackney under £2,500", "episodic", "s", ALICE,
               role="user", importance=5)
    memory.add("Has a cat called Quillbert", "semantic", "s", ALICE, importance=7)
    # escalating wording in a semantic/episodic row is never this tool's business
    memory.add("Budget is a firm, non-negotiable £1,900", "semantic", "s", ALICE, importance=7)
    for text in (DEAL_BREAKER, FIRM, ATTRIBUTED, OWN_WORD, TENTATIVE):
        memory.add(text, "reflection", "s", ALICE, importance=8)
    # Bob never said "must": the same wording is judged against HIS history, not Alice's.
    memory.add("User asked: Find a studio in Bow", "episodic", "s", BOB, role="user", importance=5)
    memory.add(OWN_WORD, "reflection", "s", BOB, importance=8)
    return memory


def _docs(memory, user, mtype):
    got = memory.col.get(where={"$and": [{"user_id": user}, {"mtype": mtype}]})
    return sorted(got.get("documents") or [])


def _run(*argv):
    out = io.StringIO()
    code = cli.purge(Path(argv[0]), apply="--apply" in argv,
                     backup=Path(argv[argv.index("--backup") + 1]) if "--backup" in argv else None,
                     out=out)
    return code, out.getvalue()


def test_each_reflection_is_judged_against_its_users_full_history(tmp_path):
    memory = _store(tmp_path)
    got = memory.col.get()
    rejected = am_mod.rejected_reflections(zip(got["ids"], got["documents"], got["metadatas"]))
    verdicts = {(row["user_id"], row["text"]): row["reason"] for row in rejected}
    assert verdicts == {
        (ALICE, DEAL_BREAKER): "escalation:deal-breaker",
        # Alice's saved row uses "firm" and "non-negotiable"; "core criteria" is still not hers
        (ALICE, FIRM): "escalation:core",
        (ALICE, ATTRIBUTED): "attribution",
        (BOB, OWN_WORD): "escalation:must",
    }
    # the guard is the R4-12 one, not a copy
    assert all(am_mod.reflection_rejection(text, "") for _user, text in verdicts)


def test_the_read_side_gives_the_same_verdict(tmp_path):
    """R5's known loss: the block beside a reflection lacked the user's own "must", so a valid
    reflection was hidden. With the user's full history it is shown; Bob's still is not."""
    memory = _store(tmp_path)
    block = [{"id": "x", "text": "Has a cat called Quillbert", "mtype": "semantic"},
             {"id": "r", "text": OWN_WORD, "mtype": "reflection"},
             {"id": "d", "text": DEAL_BREAKER, "mtype": "reflection"}]
    assert [m["id"] for m in am_mod.presentable_memories(block)] == ["x"]       # block only
    assert OWN_WORD in memory.format_for_prompt(block, user_id=ALICE)
    assert DEAL_BREAKER not in memory.format_for_prompt(block, user_id=ALICE)
    assert OWN_WORD not in memory.format_for_prompt(block, user_id=BOB)
    shown = {m["text"] for m in am_mod.presentable_memories(
        block, history=lambda: memory.user_stated_history(ALICE))}
    got = memory.col.get(where={"user_id": ALICE})
    purged = {row["text"] for row in am_mod.rejected_reflections(
        zip(got["ids"], got["documents"], got["metadatas"]))}
    assert not (shown & purged) and OWN_WORD in shown


def test_recall_memory_judges_with_the_full_history(tmp_path, monkeypatch):
    memory = _store(tmp_path)
    from core.tools import memory_tools
    monkeypatch.setattr(memory_tools, "_mem", lambda: memory)
    result = memory_tools.recall_memory_impl("cat landlord must", n=6, user_id=ALICE)
    texts = [m["text"] for m in result["memories"]]
    assert OWN_WORD in texts and DEAL_BREAKER not in texts


def test_dry_run_reports_and_deletes_nothing(tmp_path):
    memory = _store(tmp_path)
    before = memory.col.get()
    code, out = _run(str(tmp_path / "mem" / "agent_memory.sqlite3"))
    assert code == 0
    assert "DRY RUN" in out and "to delete         : 4 (2 users)" in out
    assert "reflection rows   : 6 (2 users)" in out and "kept              : 2" in out
    for row_id, text in zip(before["ids"], before["documents"]):
        if text in (DEAL_BREAKER, FIRM, ATTRIBUTED):
            assert row_id in out
    assert "reason=escalation:deal-breaker" in out and "reason=attribution" in out
    assert "x2  escalation:must" not in out and "x1  escalation:must" in out
    assert ALICE not in out and BOB not in out           # users are shown hashed
    assert memory.col.get() == before
    assert not list((tmp_path / "mem").glob("reflection-purge-backup-*.json"))


def test_apply_backs_up_then_deletes_only_rejected_reflections_and_is_idempotent(tmp_path):
    memory = _store(tmp_path)
    backup = tmp_path / "out" / "reflections.json"
    code, out = _run(str(tmp_path / "mem"), "--apply", "--backup", str(backup))
    assert code == 0, out
    assert "deleted           : 4" in out
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert sorted(row["document"] for row in saved["rows"]) == sorted(
        [DEAL_BREAKER, FIRM, ATTRIBUTED, OWN_WORD])
    assert {row["metadata"]["user_id"] for row in saved["rows"]} == {ALICE, BOB}
    assert all(row["metadata"]["mtype"] == "reflection" and row["reason"] for row in saved["rows"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    fresh = am_mod.AgentMemory(db_path=str(tmp_path / "mem"))
    assert _docs(fresh, ALICE, "reflection") == sorted([OWN_WORD, TENTATIVE])
    assert _docs(fresh, BOB, "reflection") == []
    # semantic/episodic rows, escalating wording or not, are untouched
    assert "Budget is a firm, non-negotiable £1,900" in _docs(fresh, ALICE, "semantic")
    assert ALICE_SAID_MUST in _docs(fresh, ALICE, "episodic")
    # deleted through the store: every id is tombstoned
    with sqlite3.connect(tmp_path / "mem" / "agent_memory.sqlite3") as db:
        tombstoned = {row[0] for row in db.execute("SELECT id FROM legacy_tombstones")}
    assert {row["id"] for row in saved["rows"]} <= tombstoned

    code, again = _run(str(tmp_path / "mem"), "--apply")
    assert code == 0 and "nothing to do." in again and "to delete         : 0" in again
    assert not list((tmp_path / "mem").glob("reflection-purge-backup-*.json"))


def test_default_backup_sits_beside_the_database(tmp_path):
    _store(tmp_path)
    code, out = _run(str(tmp_path / "mem"), "--apply")
    assert code == 0, out
    backups = list((tmp_path / "mem").glob("reflection-purge-backup-*.json"))
    assert len(backups) == 1 and str(backups[0]) in out


@pytest.mark.parametrize("problem", ["exists", "unwritable"])
def test_no_backup_means_no_delete(tmp_path, problem):
    memory = _store(tmp_path)
    before = memory.col.get()
    if problem == "exists":
        backup = tmp_path / "old.json"
        backup.write_text("keep me", encoding="utf-8")
    else:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        backup = locked / "reflections.json"
        if os.access(locked, os.W_OK):          # running as root: permissions do not bind
            pytest.skip("directory permissions are not enforced for this user")
    try:
        code, out = _run(str(tmp_path / "mem"), "--apply", "--backup", str(backup))
    finally:
        if problem == "unwritable":
            (tmp_path / "locked").chmod(0o700)
    assert code == 2 and "Nothing was deleted." in out
    assert am_mod.AgentMemory(db_path=str(tmp_path / "mem")).col.get() == before
    if problem == "exists":
        assert backup.read_text(encoding="utf-8") == "keep me"


def test_a_missing_database_is_never_created(tmp_path):
    code, out = _run(str(tmp_path / "nowhere" / "agent_memory.sqlite3"))
    assert code == 2 and "refusing to create a new store" in out
    assert not (tmp_path / "nowhere").exists()


def _create_legacy(root: Path, rows):
    """A minimal legacy Chroma file (the shape SQLiteMemoryCollection reads)."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "chroma.sqlite3")
    conn.executescript(
        """
        CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE segments (id TEXT PRIMARY KEY, type TEXT NOT NULL, scope TEXT NOT NULL,
                               collection TEXT NOT NULL);
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY, segment_id TEXT NOT NULL,
                                 embedding_id TEXT NOT NULL);
        CREATE TABLE embedding_metadata (id INTEGER NOT NULL, key TEXT NOT NULL,
            string_value TEXT, int_value INTEGER, float_value REAL, bool_value INTEGER,
            PRIMARY KEY (id, key));
        INSERT INTO collections(id, name) VALUES ('collection-1', 'agent_memory');
        INSERT INTO segments(id, type, scope, collection)
            VALUES ('metadata-1', 'urn:chroma:segment/metadata/sqlite', 'METADATA',
                    'collection-1');
        """)
    for internal_id, (record_id, document, metadata) in enumerate(rows, start=1):
        conn.execute("INSERT INTO embeddings(id, segment_id, embedding_id) VALUES (?, ?, ?)",
                     (internal_id, "metadata-1", record_id))
        for key, value in {"chroma:document": document, **metadata}.items():
            columns = [None, None, None, None]
            if isinstance(value, int):
                columns[1] = value
            else:
                columns[0] = str(value)
            conn.execute("INSERT INTO embedding_metadata(id, key, string_value, int_value, "
                         "float_value, bool_value) VALUES (?, ?, ?, ?, ?, ?)",
                         (internal_id, key, *columns))
    conn.commit()
    conn.close()


def test_a_purged_legacy_row_is_not_resurrected_by_the_legacy_sync(tmp_path):
    root = tmp_path / "mem"
    _create_legacy(root, [
        ("legacy-said", "User asked: Find a studio in Bow", {"user_id": BOB, "mtype": "episodic",
                                                              "role": "user"}),
        ("legacy-escalating", FIRM, {"user_id": BOB, "mtype": "reflection", "importance": 8}),
        ("legacy-tentative", TENTATIVE, {"user_id": BOB, "mtype": "reflection", "importance": 8}),
    ])
    memory = am_mod.AgentMemory(db_path=str(root))
    assert FIRM in _docs(memory, BOB, "reflection")         # imported from the legacy copy
    code, out = _run(str(root), "--apply", "--backup", str(tmp_path / "b.json"))
    assert code == 0 and "deleted           : 1" in out and "legacy copies     : 1" in out
    # a forced re-import of the unchanged legacy file, and a fresh process
    memory.col.sync_legacy(force=True)
    assert _docs(am_mod.AgentMemory(db_path=str(root)), BOB, "reflection") == [TENTATIVE]
    code, again = _run(str(root))
    assert code == 0 and "nothing to do." in again


def test_main_parses_the_documented_command_line(tmp_path, capsys):
    _store(tmp_path)
    db = str(tmp_path / "mem" / "agent_memory.sqlite3")
    assert cli.main([db]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert cli.main([db, "--apply", "--backup", str(tmp_path / "b.json")]) == 0
    assert (tmp_path / "b.json").exists()
