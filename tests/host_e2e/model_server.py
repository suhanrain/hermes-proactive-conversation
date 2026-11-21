"""One scripted model server per pytest process, shared by the host-E2E files that need it.

The host resolves the plugin's LLM client **once per process** (``agent/plugin_llm.py`` keeps the
client it built the first time), so the first ``FakeLLMServer`` a process starts is the only one the
plugin's generation calls ever reach. A second server on a second port looks healthy
and is never asked anything — its file's tests are then answered by the *first* file's script.

Per-test isolation therefore comes from re-scripting the shared server before each test (``_script()``
in each file, which also re-pins ``default_text``), never from a new port.

The per-test re-scripting below is what keeps a later host-E2E file from inheriting an earlier
file's provider script.

Files that still start their own server keep working when they are the first (and only) host-E2E file
in the process. Making the *whole* directory safe this way is a harness-wide change (all thirteen
files would have to share one server); that is deliberately not done here.
"""

from __future__ import annotations

from typing import Any, Optional

_SERVER: Optional[Any] = None


def shared(default_text: str) -> Any:
    """The process's one :class:`FakeLLMServer`, started on first use.

    Not stopped: there is exactly one per process, and pytest's exit is what ends it. Each caller
    re-pins ``default_text`` so the file that is running decides what an unscripted main turn answers.
    """
    global _SERVER
    if _SERVER is None:
        from tests.fakes.fake_llm_provider import FakeLLMServer

        server = FakeLLMServer(default_text=default_text)
        server.start()
        _SERVER = server
    _SERVER.default_text = default_text
    return _SERVER
