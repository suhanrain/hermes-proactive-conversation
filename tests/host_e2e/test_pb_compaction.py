"""The **measured** compaction boundary of the host-available transcript.

"Complete conversation" means the session history the host can actually supply **at that moment** —
*possibly already compacted* — **not** literal uncompressed lifetime history. The compaction
boundary is **measured in a fixture** (and, separately and only with operator authorisation, on a
real profile), never assumed.

This file is that fixture measurement for the isolated host. It drives the **real** read-only
transcript seam (:func:`proactive.host_adapter.recent_transcript`, the ``_CONTEXT_SQL`` with its
``compacted = 0`` predicate) against a throwaway SQLite ``messages`` table and records exactly where
the boundary falls:

* which seeded rows the seam **excludes** and why (compacted / inactive / blank / non-chat role);
* how the ``limit`` window truncates a **tail**, so an *older but still non-compacted* row falls out
  of the window even though it was never compacted — the two loss mechanisms are distinct;
* the coercion rules for ``limit`` (a bool, a fraction, an out-of-bounds value, a non-number each
  fall back to the documented default :data:`proactive.contract.SESSION_CONTEXT_ROUNDS_DEFAULT`);
* the **newest user row** the recogniser's durable fallback keys on, and its stable digest.

Measured posture
----------------
These are *assertions of measured facts*, not assumptions. The file goes red the moment the seam
stops excluding compacted rows (the ``_CONTEXT_SQL`` guard case below), stops truncating as a tail,
or stops coercing ``limit`` the way :mod:`proactive.settings` does — i.e. it fails if you *assume* a
wider history than the host actually supplies.

Scope: no live profile, no gateway, no host source edit. Every row here is a throwaway ``tmp_path``
SQLite table; ``support`` pins a throwaway ``HERMES_HOME`` before any hermes module loads.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TESTS_DIR = _HERE.parent
for _p in (str(_TESTS_DIR), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

import support  # noqa: E402  (pins a throwaway HERMES_HOME before any hermes module is imported)

from proactive import contract as C          # noqa: E402
from proactive import host_adapter            # noqa: E402

SID = "sid-compaction-1"


def _seed_messages(path, session_id, rows):
    """Create a real ``messages`` table and insert ``rows``.

    Each row is ``(role, content, active, compacted)``; the insert order is the id order, so the
    last row inserted is the newest by ``ORDER BY id DESC``.
    """
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
        "content TEXT, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0)")
    for role, content, active, compacted in rows:
        con.execute(
            "INSERT INTO messages (session_id, role, content, active, compacted) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, int(active), int(compacted)))
    con.commit()
    con.close()
    return path


def _fixture_rows():
    """The seeded history, oldest → newest.

    Deliberately mixes every exclusion kind the seam carries, and places a **compacted but newer**
    row last so exclusion is proven to be by flag, not by recency:

    ===== ============== =========  ======== ==================================
    order role           content    flags    expected in the seam's window?
    ===== ============== =========  ======== ==================================
    1     user           OLD-USER   compact  no  (compacted = 1)
    2     assistant      OLD-A      compact  no  (compacted = 1)
    3     user           INACTIVE   active 0 no  (active = 0)
    4     assistant      "   "      live     no  (TRIM(content) = '')
    5     system         SYS-MSG    live     no  (role not user/assistant)
    6     user           KEEP-USER  live     YES
    7     assistant      KEEP-A     live     YES
    8     user           NEWEST-USER live    YES
    9     user           COMPACTED-NEW compact no (compacted = 1, newest id)
    ===== ============== =========  ======== ==================================
    """
    return [
        ("user", "OLD-USER", 1, 1),
        ("assistant", "OLD-A", 1, 1),
        ("user", "INACTIVE-USER", 0, 0),
        ("assistant", "   ", 1, 0),
        ("system", "SYS-MSG", 1, 0),
        ("user", "KEEP-USER", 1, 0),
        ("assistant", "KEEP-A", 1, 0),
        ("user", "NEWEST-USER", 1, 0),
        ("user", "COMPACTED-NEW", 1, 1),
    ]


_LIVE_WINDOW = [("user", "KEEP-USER"), ("assistant", "KEEP-A"), ("user", "NEWEST-USER")]


# ── reading 1: exclusion boundary — what compaction hides ─────────────────────────────────────


def test_the_window_excludes_compacted_inactive_blank_and_non_chat_rows(tmp_path):
    """Measured: only live user/assistant rows survive; compacted rows are invisible to the seam.

    The core reading: a *compacted* row — even the newest one — is absent from the window the model
    would receive, so "complete conversation" is the possibly-compacted history, not lifetime
    history.
    """
    db = _seed_messages(tmp_path / "state.db", SID, _fixture_rows())
    window = host_adapter.recent_transcript(SID, 10, db_path=db)
    assert window == _LIVE_WINDOW, (
        "the seam must return exactly the three live user/assistant rows, oldest-first; "
        f"got {window!r}")
    texts = [t for _, t in window]
    for hidden in ("OLD-USER", "OLD-A", "INACTIVE-USER", "SYS-MSG", "COMPACTED-NEW"):
        assert hidden not in texts, f"{hidden} must not reach the window (excluded by the seam)"
    # And the newest compacted row proves exclusion is by flag, not recency.
    assert "COMPACTED-NEW" not in texts


def test_compaction_boundary_is_the_tail_not_the_lifetime(tmp_path):
    """Measured: the window is a *tail* of live rows — an older live row falls out at the limit.

    This separates the two loss mechanisms: compaction removes a row by *flag*; the ``limit``
    removes it by *window size*. Both must be measured, not conflated.
    """
    db = _seed_messages(tmp_path / "state.db", SID, _fixture_rows())
    only_two = host_adapter.recent_transcript(SID, 2, db_path=db)
    # exactly the newest two live rows, oldest-first -- KEEP-USER (older but live) is out of window
    assert only_two == [("assistant", "KEEP-A"), ("user", "NEWEST-USER")], only_two


# ── reading 2: the ``limit`` coercion boundary (matches proactive.settings) ───────────────────


@pytest.mark.parametrize("bad_limit", [True, False, 0, -5, 2.5, 51, 10_000, "3", None, 3.0 + 0.5])
def test_an_unusable_limit_falls_back_to_the_documented_default(tmp_path, bad_limit):
    """Measured: a bool / fraction / out-of-bound / non-number limit → the default window (10).

    With the fixture's three live rows the whole live window is returned, i.e. the value was
    *coerced to the default*, never trusted as a licence to change the boundary.
    """
    db = _seed_messages(tmp_path / "state.db", SID, _fixture_rows())
    window = host_adapter.recent_transcript(SID, bad_limit, db_path=db)
    assert window == _LIVE_WINDOW, f"limit={bad_limit!r} should fall back to default; got {window!r}"
    assert C.SESSION_CONTEXT_ROUNDS_DEFAULT == 10


def test_an_in_bound_limit_is_honoured(tmp_path):
    """Measured: an integer inside [1, SESSION_CONTEXT_ROUNDS_MAX] is used verbatim (tail of that many)."""
    db = _seed_messages(tmp_path / "state.db", SID, _fixture_rows())
    assert host_adapter.recent_transcript(SID, 1, db_path=db) == [("user", "NEWEST-USER")]
    assert host_adapter.recent_transcript(SID, C.SESSION_CONTEXT_ROUNDS_MAX, db_path=db) == _LIVE_WINDOW


# ── reading 3: unknown/absent store is an empty window (never a crash) ───────────────────────


def test_no_session_or_no_db_path_is_an_empty_window(tmp_path):
    db = _seed_messages(tmp_path / "state.db", SID, _fixture_rows())
    assert host_adapter.recent_transcript("", 10, db_path=db) == []
    assert host_adapter.recent_transcript("sid-unknown", 10, db_path=db) == []
    assert host_adapter.recent_transcript(SID, 10, db_path=None) == []
    # a database that is not a messages table must not raise, it answers []
    bad = tmp_path / "empty.db"
    sqlite3.connect(str(bad)).close()
    assert host_adapter.recent_transcript(SID, 10, db_path=bad) == []


# ── reading 4: the newest user row + its digest (the recogniser's durable key) ───────────────


def test_the_newest_live_user_row_and_its_digest_are_measurable(tmp_path):
    """Measured: the newest live user row is ``NEWEST-USER`` (not the compacted-newer row), and its
    stripped-bytes SHA-256 is a stable 64-hex digest — the value the durable pin re-recognises on
    restart."""
    db = _seed_messages(tmp_path / "state.db", SID, _fixture_rows())
    window = host_adapter.recent_transcript(SID, 10, db_path=db)
    user_rows = [t for role, t in window if role == "user"]
    newest_user = user_rows[-1]
    assert newest_user == "NEWEST-USER"
    digest = hashlib.sha256(newest_user.strip().encode("utf-8")).hexdigest()
    assert len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest)
    # stable, reproducible digest of the literal fixture text
    assert digest == hashlib.sha256(b"NEWEST-USER").hexdigest()


# ── guard: the exclusion is a predicate in the seam, not an accident of this fixture ──────────


def test_the_context_sql_carries_the_compaction_predicate_and_active_and_trim():
    """The seam's SQL must keep ``compacted = 0`` (and ``active = 1`` / ``TRIM``).

    If someone drops the compaction predicate the behavioural reading above goes red; this assertion
    states the invariant directly so the *mechanism* of the boundary is locked, not just its effect.
    """
    sql = host_adapter._CONTEXT_SQL
    assert "compacted = 0" in sql, "the seam must exclude compacted rows (Q-013 ruling (a))"
    assert "active = 1" in sql
    assert "TRIM(content) <> ''" in sql
    assert "role IN ('user', 'assistant')" in sql
