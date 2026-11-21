"""The two real read-only sources, read from data Hermes itself wrote.

What makes these tests *real*: the transcript rows are written by ``SessionDB.create_session`` /
``append_message`` into the isolated home's own ``state.db`` (real schema, real writer), and the
memory file is written by the real ``MemoryStore``. Nothing here fabricates a record: every
fingerprint asserted below is recomputed from the bytes on disk, and the transcript checks read the
row back by its primary key.

Isolation: ``support`` pins ``HERMES_HOME`` to a throwaway directory before any hermes module loads,
so these tests never touch a live profile's database or memory files.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

import p6a_support as S
from proactive import content, contract as C
from proactive.sources import (CrossSessionMemorySource, EXCLUDED_DISPLAY_KINDS,
                               SessionTranscriptSource, codes, open_read_only, parse_entries,
                               read_only_uri)
from proactive.sources.memory import ENTRY_DELIMITER

NOW = 1_800_000_000.0
DAY = 86400.0


@pytest.fixture(autouse=True)
def _clean_memory_files():
    """The isolated home is shared by the whole pytest session: each test starts with no memory.

    Without this, one test's memory entries would be "recent memory" for the next test's adapter.
    """
    for name in ("MEMORY.md", "USER.md"):
        (S.memory_dir() / name).unlink(missing_ok=True)
    yield
    for name in ("MEMORY.md", "USER.md"):
        (S.memory_dir() / name).unlink(missing_ok=True)


USER_ROWS = ("我周五要和面试官聊", "腰还是有点酸", "想买个新键盘", "周五和老王吃饭")


def transcript(*, profile="dev", max_messages=50, window_seconds=30 * DAY, clock=lambda: NOW):
    return SessionTranscriptSource(S.db_path(), max_messages=max_messages,
                                   window_seconds=window_seconds, profile=profile, clock=clock)


def seed(tag, rows=None, *, session_key=None, profile="dev"):
    """Create one real session with real message rows; returns (session_id, session_key, texts)."""
    sid = "p6a_" + tag
    key = session_key or f"agent:dev:feishu:dm:{tag}"
    rows = rows if rows is not None else [
        ("assistant", "嗯", {"timestamp": NOW - 5}),
        ("user", USER_ROWS[3], {"timestamp": NOW - 40}),
        ("user", USER_ROWS[2], {"timestamp": NOW - 30}),
        ("tool", "{}", {"timestamp": NOW - 25}),
        ("assistant", "好的", {"timestamp": NOW - 22}),
        ("user", USER_ROWS[1], {"timestamp": NOW - 20}),
        ("user", USER_ROWS[0], {"timestamp": NOW - 10}),
        ("user", "这是机器回合", {"timestamp": NOW - 9, "display_kind": "internal_notification"}),
        ("assistant", "隐藏回合", {"timestamp": NOW - 8, "display_kind": "hidden"}),
        ("user", "失败回合", {"timestamp": NOW - 7, "display_kind": "failed_turn"}),
    ]
    S.seed_transcript(S.session_db(), session_id=sid, session_key=key, profile=profile, rows=rows)
    return sid, key


# ── this session's transcript ───────────────────────────────────────────────────────────────


def test_transcript_reads_the_real_rows_newest_first_and_nothing_else():
    sid, key = seed(S.unique("t1"))
    records = transcript().read(S.scope_for(session_key=key, session_id=sid, profile="dev"))
    assert [r.text for r in records] == [USER_ROWS[0], USER_ROWS[1], USER_ROWS[2], USER_ROWS[3]]
    assert all(r.source == C.SOURCE_SESSION and r.subject == "user" for r in records)
    assert [r.at for r in records] == [NOW - 10, NOW - 20, NOW - 30, NOW - 40]


def test_every_fingerprint_resolves_to_the_row_it_came_from():
    """At least 3 (fingerprint <-> source position) pairs, each recomputed from the database."""
    sid, key = seed(S.unique("t2"))
    records = transcript().read(S.scope_for(session_key=key, session_id=sid, profile="dev"))
    assert len(records) >= 3
    conn = open_read_only(S.db_path())
    try:
        pairs = 0
        for record in records:
            row_id = int(record.record_id.split("#", 1)[1])
            stored = conn.execute("SELECT content, session_id FROM messages WHERE id = ?",
                                  (row_id,)).fetchone()
            assert stored is not None, record.record_id
            content_text, row_session = stored
            assert row_session == sid
            assert record.text == content_text
            assert record.fingerprint() == (
                f"{C.SOURCE_SESSION}:{record.record_id}:{S.text_sha256(content_text)[:8]}")
            pairs += 1
        print(f"[P6a transcript] rows={len(conn.execute('SELECT id FROM messages WHERE session_id=?', (sid,)).fetchall())} "
              f"read={len(records)} fingerprint_pairs={pairs}")
        assert pairs >= 3
    finally:
        conn.close()


def test_assistant_tool_and_machinery_rows_are_not_quotable_topics():
    sid, key = seed(S.unique("t3"))
    records = transcript().read(S.scope_for(session_key=key, session_id=sid, profile="dev"))
    texts = {r.text for r in records}
    assert "嗯" not in texts and "好的" not in texts and "{}" not in texts
    for marker in ("这是机器回合", "隐藏回合", "失败回合"):
        assert marker not in texts, marker
    assert EXCLUDED_DISPLAY_KINDS == ("internal_notification", "hidden", "failed_turn")


def test_both_window_values_are_configurable_and_the_smaller_one_wins(tmp_path):
    sid, key = seed(S.unique("t4"))
    scope = S.scope_for(session_key=key, session_id=sid, profile="dev")
    by_count = SessionTranscriptSource(S.db_path(), max_messages=2, window_seconds=30 * DAY,
                                      profile="dev", clock=lambda: NOW).read(scope)
    assert [r.text for r in by_count] == [USER_ROWS[0], USER_ROWS[1]]
    by_time = SessionTranscriptSource(S.db_path(), max_messages=50, window_seconds=25.0,
                                      profile="dev", clock=lambda: NOW).read(scope)
    assert [r.text for r in by_time] == [USER_ROWS[0], USER_ROWS[1]]
    # A window in the past (clock moved forward) is the negative control: nothing is recent.
    later = SessionTranscriptSource(S.db_path(), max_messages=50, window_seconds=5.0,
                                    profile="dev", clock=lambda: NOW + 3600).read(scope)
    assert later == ()
    for bad in (0, -1, True, "50"):
        with pytest.raises(ValueError):
            SessionTranscriptSource(S.db_path(), max_messages=bad, window_seconds=30 * DAY)


def test_a_scope_that_does_not_describe_this_session_cannot_read_it():
    sid, key = seed(S.unique("t5"))
    other_sid, other_key = seed(S.unique("t5b"))
    src = transcript()
    with pytest.raises(content.SourceUnavailable) as missing:
        src.read(S.scope_for(session_key=key, session_id="p6a_does_not_exist", profile="dev"))
    assert missing.value.reason == codes.UNAVAILABLE_SESSION_UNKNOWN
    # Another session's id, but this session's key: the row exists and belongs to another key.
    with pytest.raises(content.SourceUnavailable) as mismatched:
        src.read(S.scope_for(session_key=key, session_id=other_sid, profile="dev"))
    assert mismatched.value.reason == codes.UNAVAILABLE_SCOPE_MISMATCH
    with pytest.raises(content.SourceUnavailable) as key_mismatch:
        src.read(S.scope_for(session_key=other_key, session_id=sid, profile="dev"))
    assert key_mismatch.value.reason == codes.UNAVAILABLE_SCOPE_MISMATCH
    # The profile the database belongs to is part of the identity, not a hint.
    with pytest.raises(content.SourceUnavailable) as profile_mismatch:
        SessionTranscriptSource(S.db_path(), max_messages=50, window_seconds=30 * DAY,
                                profile="someone-else", clock=lambda: NOW).read(
            S.scope_for(session_key=key, session_id=sid, profile="dev"))
    assert profile_mismatch.value.reason == codes.UNAVAILABLE_SCOPE_MISMATCH
    assert src.calls == 3


def test_a_database_that_cannot_be_opened_is_unavailable_not_empty(tmp_path):
    src = SessionTranscriptSource(tmp_path / "no-such.db", max_messages=50,
                                  window_seconds=30 * DAY, profile="dev", clock=lambda: NOW)
    with pytest.raises(content.SourceUnavailable) as failed:
        src.read(S.scope_for(session_key="agent:dev:feishu:dm:x", session_id="any", profile="dev"))
    assert failed.value.reason == C.UNAVAILABLE_READ_FAILED
    # Same answer for a file that is not a database at all.
    junk = tmp_path / "junk.db"
    junk.write_text("not a database", encoding="utf-8")
    broken = SessionTranscriptSource(junk, max_messages=50, window_seconds=30 * DAY,
                                     profile="dev", clock=lambda: NOW)
    with pytest.raises(content.SourceUnavailable) as not_a_db:
        broken.read(S.scope_for(session_key="agent:dev:feishu:dm:x", session_id="any", profile="dev"))
    assert not_a_db.value.reason == C.UNAVAILABLE_READ_FAILED


def test_the_read_is_read_only_on_the_connection_and_on_disk():
    """The read path cannot write — proven on the connection and on the file's bytes."""
    sid, key = seed(S.unique("t6"))
    before = S.db_path().read_bytes()
    conn = open_read_only(S.db_path())
    try:
        assert read_only_uri(S.db_path()).endswith("?mode=ro")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE p6a_probe (i INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM messages")
    finally:
        conn.close()
    records = transcript().read(S.scope_for(session_key=key, session_id=sid, profile="dev"))
    assert records
    assert S.db_path().read_bytes() == before, "a read must not modify the database file"


# ── cross-session memory ────────────────────────────────────────────────────────────────────


def test_memory_reads_the_real_files_and_matches_hermes_own_parser():
    path = S.write_memory(["用户在做减脂，蛋白目标 1.6-2g/kg", "周五和老王吃饭"], when=NOW - 100)
    assert ENTRY_DELIMITER == __import__("tools.memory_tool_store", fromlist=["x"]).ENTRY_DELIMITER
    entries = S.memory_entries(path)
    assert parse_entries(path.read_text(encoding="utf-8-sig")) == entries
    src = CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                  clock=lambda: NOW)
    records = src.read(S.scope_for(session_key="agent:dev:feishu:dm:m", session_id="s", profile="dev"))
    assert [r.text for r in records] == entries
    assert all(r.record_id.startswith("MEMORY.md#") and r.source == C.SOURCE_MEMORY for r in records)
    assert {r.at for r in records} == {path.stat().st_mtime}
    for record in records:
        assert record.fingerprint() == (
            f"{C.SOURCE_MEMORY}:{record.record_id}:{S.text_sha256(record.text)[:8]}")


def test_a_bom_does_not_glue_itself_to_the_first_entry(tmp_path):
    path = S.memory_dir() / "USER.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\ufeff用户是张伟", encoding="utf-8")
    os.utime(path, (NOW - 5, NOW - 5))
    src = CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                  files=("USER.md",), clock=lambda: NOW)
    records = src.read(S.scope_for(session_key="k", session_id="s", profile="dev"))
    assert [r.text for r in records] == ["用户是张伟"]


def test_memory_window_missing_files_and_unreadable_files():
    S.write_memory(["很久以前的一条"], when=NOW - 40 * DAY)
    old = CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                  clock=lambda: NOW).read(
        S.scope_for(session_key="k", session_id="s", profile="dev"))
    assert old == (), "a file older than the window is not recent memory"
    fresh = CrossSessionMemorySource(S.memory_dir(), window_seconds=41 * DAY, profile="dev",
                                    clock=lambda: NOW).read(
        S.scope_for(session_key="k", session_id="s", profile="dev"))
    assert [r.text for r in fresh] == ["很久以前的一条"]
    # A profile with no memory yet: readable, zero records — not an error.
    empty = CrossSessionMemorySource(S.iso_home() / "no-memories", window_seconds=30 * DAY,
                                     profile="dev", clock=lambda: NOW).read(
        S.scope_for(session_key="k", session_id="s", profile="dev"))
    assert empty == ()
    # A file whose bytes cannot be read is a read failure, never "no memory".
    path = S.memory_dir() / "MEMORY.md"
    path.write_bytes(b"\xff\xfe\x00broken utf-8 \xff")
    os.utime(path, (NOW - 5, NOW - 5))
    with pytest.raises(content.SourceUnavailable) as unreadable:
        CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                 clock=lambda: NOW).read(
            S.scope_for(session_key="k", session_id="s", profile="dev"))
    assert unreadable.value.reason == C.UNAVAILABLE_READ_FAILED


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating a symlink needs SeCreateSymbolicLinkPrivilege on Windows; the escape case "
           "is verified on POSIX CI",
)
def test_a_memory_file_that_resolves_outside_the_memory_dir_is_refused(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("别的 profile 的记忆", encoding="utf-8")
    path = S.memory_dir() / "MEMORY.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.symlink_to(outside)
    os.utime(outside, (NOW - 5, NOW - 5))
    try:
        with pytest.raises(content.SourceUnavailable) as refused:
            CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                     clock=lambda: NOW).read(
                S.scope_for(session_key="k", session_id="s", profile="dev"))
        assert refused.value.reason == C.UNAVAILABLE_READ_FAILED
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits and os.geteuid() do not express an unreadable file on Windows; the "
           "permission case is verified on POSIX CI",
)
def test_a_file_that_cannot_be_permission_read_is_a_read_failure():
    if os.geteuid() == 0:  # pragma: no cover - root ignores the mode bits
        pytest.skip("running as root: file permissions cannot express this case")
    path = S.memory_dir() / "MEMORY.md"
    path.write_text("读不到的一条", encoding="utf-8")
    os.utime(path, (NOW - 5, NOW - 5))
    os.chmod(path, 0)
    try:
        with pytest.raises(content.SourceUnavailable) as refused:
            CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                     clock=lambda: NOW).read(
                S.scope_for(session_key="k", session_id="s", profile="dev"))
        assert refused.value.reason == C.UNAVAILABLE_READ_FAILED
    finally:
        os.chmod(path, 0o644)


def test_another_profile_scope_is_refused_before_any_file_is_opened():
    src = CrossSessionMemorySource(S.memory_dir(), window_seconds=30 * DAY, profile="dev",
                                  clock=lambda: NOW)
    with pytest.raises(content.SourceUnavailable) as refused:
        src.read(S.scope_for(session_key="k", session_id="s", profile="work"))
    assert refused.value.reason == codes.UNAVAILABLE_SCOPE_MISMATCH
    assert src.calls == 1
