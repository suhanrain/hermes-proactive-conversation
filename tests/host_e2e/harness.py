"""End-to-end harness — the installed-form chain, on an isolated host.

What is REAL here (and therefore what a green run means):

* the plugin **loader**: the package is copied to ``<ISO_HOME>/plugins/proactive-conversation`` and found by
  hermes' own ``discover_plugins()``, so the two registered callbacks are the code in this
  repository, not a stand-in;
* the **wake scheduling**: ``GatewayGoalsMixin._heartbeat_poll_once`` → ``_heartbeat_poll_watch``
  (due-tick claim on a real ``heartbeat:<session_id>`` SessionDB row, synthetic event,
  ``adapter.handle_message``, tick refund in ``finally``);
* the **inbound lane**: ``BasePlatformAdapter.handle_message`` → ``_process_message_background`` →
  ``GatewayRunner._handle_message`` → the real ``pre_gateway_dispatch`` invocation → auth → idle
  dispatch lane → the real turn;
* the **turn**: a real ``AIAgent`` against ``tests.fakes.fake_llm_provider`` (loopback, scripted), so
  ``agent.turn_finalizer.apply_llm_output_transform`` — the seam the plugin's delivery gate hooks —
  runs for real, and the turn's own rows are persisted for real;
* the **delivery decision**: ``GatewayRunner._hmwa_shape_agent_response`` →
  ``gateway.response_filters.is_intentional_silence_agent_result`` (the ``[SILENT]`` verdict) and
  ``_hmwa_deliver_turn_response``;
* the **session store**: a real ``SessionStore`` (journal + per-session metadata) over the real
  SessionDB.

What is NOT real, so no reader over-reads a green run:

* the **transport**: ``FakeFeishuAdapter`` is a real ``BasePlatformAdapter`` subclass whose ``send``
  records into ``wire`` instead of calling the Feishu API. "Zero sends" is a count over that list; no
  network call happens and no Feishu token exists anywhere in this suite;
* the **model**: the scripted loopback server. ``wire``/``llm_calls()`` still mean "the real client
  issued nothing", not "a stub was told to stay quiet";
* the **unload clock**: the plugin's own clock/RNG are injectable seams (``runtime.set_clock`` /
  ``set_rng``), used so a 24h silence window is exercised in milliseconds. Wall-clock behaviour is
  untouched;
* the runner is built with ``GatewayRunner.__new__`` (no ``__init__``), so the 5s poll *loop* never
  starts: each :meth:`IsolatedHost.poll` is one real invocation of ``_heartbeat_poll_once``;
* two profile-scope wrappers are bypassed: the adapter's ``_primary_message_handler()`` →
  ``_standalone_scoped(...)`` by binding ``_handle_message`` to the adapter directly, and
  ``_profile_scope_for_source`` by a null context (this harness hosts one profile — the standalone
  gateway case). Admission, the hook, auth, the idle lane, the turn and delivery are production
  code.

Isolation: ``support`` pins ``HERMES_HOME`` to a throwaway temp dir before any hermes module
imports, and this harness only ever writes ``<ISO_HOME>/config.yaml``. No live profile is read or
written, no plugin is installed or enabled anywhere else, the only socket is the loopback fake
provider, and nothing is pushed anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

# ``support`` pins HERMES_HOME at import time, so it must be imported before any hermes module.
_TESTS_DIR = Path(__file__).resolve().parents[1]
_HERE = Path(__file__).resolve().parent
for _path in (str(_TESTS_DIR), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import support  # noqa: E402  (importing it pins HERMES_HOME)

PLUGIN_NAME = "proactive-conversation"
META_KEY = "proactive-conversation:session"

#: Sentinel for the metadata read: the store returns the default only when the key is *absent*, so
#: this object (never a JSON value) tells "no key at all" apart from a key holding ``null``.
_ABSENT = object()
PROBE_PLUGIN = "p2-hook-probe"
HOOK = "pre_gateway_dispatch"


def agent_repo_root() -> Path:
    """The hermes-agent checkout this interpreter uses (it provides ``hermes_cli``, ``gateway``)."""
    import hermes_cli

    return Path(hermes_cli.__file__).resolve().parents[1]


def ensure_agent_importable() -> Path:
    """Put the hermes-agent checkout on ``sys.path`` so its scripted provider is importable."""
    root = agent_repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


# ── isolated home: config + plugin install + real discovery ───────────────────────────────────


def write_home_config(base_url: str, *, isolation: str = "in_process", enabled: bool = True,
                      extra_plugins=(), extra: str = "") -> Path:
    """Write ``<ISO_HOME>/config.yaml`` for one scenario (the real config of a throwaway profile).

    ``isolation`` is the operator-level ``plugins.isolation`` value and ``enabled``/``extra_plugins``
    the profile's ``plugins.enabled`` list — the knobs a real install has.
    """
    ensure_agent_importable()
    from tests.fakes.fake_llm_provider import write_hermes_home

    names = [PLUGIN_NAME] if enabled else []
    names += [name for name in extra_plugins if name not in names]
    enabled_block = f"  enabled: [{', '.join(names)}]\n"
    plugins_block = "plugins:\n" + enabled_block + f"  isolation: {isolation}\n"
    return write_hermes_home(support.ISO_HOME, base_url, extra_config=plugins_block + extra)


def install_plugin_tree() -> Path:
    """The plugin as installed under the isolated home (files copied from this package)."""
    return support.install_plugin()


def discover_installed_plugin():
    """Discover plugins through hermes' real loader; return the loaded entry for this plugin."""
    import hermes_cli.plugins as plugins_mod

    plugins_mod._reset_plugin_managers_for_tests()
    plugins_mod.discover_plugins()
    manager = plugins_mod.get_plugin_manager()
    loaded = [p for p in manager._plugins.values() if p.manifest.name == PLUGIN_NAME]
    assert loaded, (
        "the plugin was not discovered from the isolated home: "
        f"{[(p.manifest.name, p.enabled, p.error) for p in manager._plugins.values()]}"
    )
    return loaded[0], manager


def hook_callbacks(name: str = HOOK) -> List[Any]:
    """The live callbacks hermes dispatch will actually call for ``name``."""
    import hermes_cli.plugins as plugins_mod

    manager = plugins_mod.get_plugin_manager()
    return list(manager._hooks.get(name) or [])


#: Substring of the module the plugin-under-test's callbacks live in (probe plugin excluded).
PLUGIN_MODULE_HINT = "proactive_conversation"


def plugin_callback(name: str = HOOK) -> Any:
    """The live callback registered by the plugin under test (not by the probe)."""
    for cb in hook_callbacks(name):
        if PLUGIN_MODULE_HINT in getattr(cb, "__module__", ""):
            return cb
    return None


def hooks_are_in_process() -> bool:
    """True when the plugin's live callbacks are this process's own modules.

    Under ``plugins.isolation: host`` the registered callbacks are parent-side proxies whose
    ``__module__`` is the child's module name, which does not exist in this process's
    ``sys.modules`` — that difference is the observable form of "loaded in the plugin host".
    """
    callback = plugin_callback()
    return callback is not None and callback.__module__ in sys.modules


def shutdown_plugin_host() -> None:
    """Stop the per-profile plugin host (if one started) and drop every cached manager."""
    import hermes_cli.plugins as plugins_mod

    manager = plugins_mod.get_plugin_manager()
    host = getattr(manager, "_plugin_host_instance", None)
    if host is not None:
        with contextlib.suppress(Exception):
            host.shutdown()
    plugins_mod._reset_plugin_managers_for_tests()


# ── the fake Feishu adapter (a real BasePlatformAdapter; only the transport is ours) ──────────


class FakeFeishuAdapterBase:
    """Marker base so tests can name the adapter type without importing it lazily."""


def build_adapter(runner):
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class FakeFeishuAdapter(BasePlatformAdapter):
        """Fake Feishu DM transport: real adapter base, real inbound lane, sends land in ``wire``."""

        def __init__(self, config, platform):
            super().__init__(config, platform)
            self.wire: List[Dict[str, Any]] = []
            self.delivery_attempts: List[Dict[str, Any]] = []

        async def connect(self, *, is_reconnect=False):
            return True

        async def disconnect(self):
            return None

        async def get_chat_info(self, chat_id):
            return {"chat_id": str(chat_id), "chat_type": "dm"}

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.delivery_attempts.append({"chat_id": str(chat_id), "content": content})
            if content:
                self.wire.append({"chat_id": str(chat_id), "content": content})
            return SendResult(success=True, message_id=f"feishu-{len(self.delivery_attempts)}")

    adapter = FakeFeishuAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.FEISHU)
    adapter.gateway_runner = runner
    # Production binds `_primary_message_handler()` → `_standalone_scoped(self._handle_message)`;
    # only that profile-scope wrapper is skipped (see the module docstring).
    adapter.set_message_handler(runner._handle_message)
    runner._delivery_adapter_for = lambda source: adapter
    return adapter


# ── the real runner, over an isolated store + SessionDB ───────────────────────────────────────


def build_runner(store, db):
    import gateway.run as gateway_run
    from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig

    config = GatewayConfig()
    config.platforms[Platform.FEISHU] = PlatformConfig(
        enabled=True, typing_indicator=False,
        home_channel=HomeChannel(platform=Platform.FEISHU, chat_id="ou_p2", name="p2-home"),
    )
    runner = gateway_run.GatewayRunner.__new__(gateway_run.GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner._session_db = db
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._auth_calls = []

    def _authorized(source):
        runner._auth_calls.append(getattr(source, "user_id", None))
        return True

    runner._is_user_authorized_for_source = _authorized
    runner._set_session_env = lambda _context: ("env-token",)
    runner._clear_session_env = lambda _tokens: None
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._should_send_voice_reply = lambda *a, **k: False
    # One profile is hosted here: the scope wrapper is a no-op, as for a standalone gateway.
    runner._profile_scope_for_source = lambda source: contextlib.nullcontext()
    return runner


#: The verdict fields production's ``_run_agent`` hands the gateway's failure classification
#: (``gateway/run_turn.py``: ``_hmwa_shape_agent_response`` / ``_hmwa_classify_turn_failure``).
#: The seam below carries them, so a harness turn cannot pass for a success the gateway could not
#: tell apart from a failed one.
AGENT_VERDICT_FIELDS = ("failed", "completed", "interrupted", "partial", "error", "failure_reason")


def bind_real_agent(runner, db, session_id: str, base_url: str, captured: Dict[str, Any],
                    *, platform: str = "feishu", result_patch=None) -> None:
    """Bind ``runner._run_agent`` to a REAL ``AIAgent`` turn against the scripted provider.

    The returned dict stands where production's ``GatewayRunner._run_agent`` stands, so it carries the
    agent's real **verdict** (``AGENT_VERDICT_FIELDS``) — whether the turn failed, was interrupted or
    came back partial is what the gateway's shaping, its failure notice and its transcript persistence
    read. Only the transcript-shaped fields stay empty (``messages``/``tools``/``history_offset``):
    the gateway's own persistence is what writes this session's rows, in the harness as in production.

    ``result_patch`` is the fault-injection seam for a failure stamped **after** the turn produced its
    text. The production shape at this same seam is ``_run_agent_timeout_result``
    (``gateway/run_turn.py``: the gateway gives up on an idle turn and substitutes a synthetic
    ``failed=True`` dict for the real result), so a test can reproduce "the model's text already went
    through the transform seam, and the gateway failed the round anyway" without touching the plugin.
    A patch receives the real result dict and returns what the gateway should see.
    """
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools

    async def _run(**kwargs):
        from run_agent import AIAgent  # imported at first turn: keeps home guards quiet

        captured.setdefault("turns", []).append(kwargs)
        toolsets = sorted(_get_platform_tools(load_config(), platform))
        agent = AIAgent(
            provider="custom", base_url=base_url, api_key="sk-p2-fake", model="fake-model",
            session_db=db, session_id=session_id, quiet_mode=True, platform=platform,
            enabled_toolsets=toolsets,
        )
        result = await asyncio.to_thread(
            agent.run_conversation, kwargs["message"],
            conversation_history=kwargs["history"], task_id=session_id,
        )
        captured.setdefault("results", []).append(result)
        shaped = {
            "final_response": result.get("final_response"),
            "messages": [],
            "tools": [],
            "history_offset": len(kwargs["history"] or []),
            "last_prompt_tokens": 0,
            "api_calls": result.get("api_calls", 0),
            "agent_persisted": True,
        }
        shaped.update({key: result[key] for key in AGENT_VERDICT_FIELDS if key in result})
        if result_patch is not None:
            shaped = result_patch(shaped) or shaped
        captured["shaped_results"] = captured.get("shaped_results", []) + [shaped]
        return shaped

    runner._run_agent = _run


def counting_agent_body(runner) -> List[Any]:
    """A turn seam that only counts: proves no turn ran without needing the agent stack."""
    calls: List[Any] = []

    async def _run(**kwargs):
        calls.append(kwargs.get("message"))
        return {"final_response": "SHOULD-NOT-RUN", "messages": [], "tools": [],
                "history_offset": 0, "last_prompt_tokens": 0, "api_calls": 0,
                "agent_persisted": True}

    runner._run_agent = _run
    return calls


# ── session db / store plumbing ───────────────────────────────────────────────────────────────


def feishu_dm(chat_id: str = "ou_42"):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.FEISHU, chat_id=chat_id, user_id=chat_id, chat_type="dm")


def unique_source(tag: str):
    """A Feishu DM whose chat id is unique to one test.

    Every host in a pytest session shares the isolated home's ``state.db``, and ``SessionStore``
    revives the session it already knows for a chat key — so two tests reusing ``ou_42`` would share
    one session (and one transcript). Giving each host its own chat keeps the per-test measurement
    scoped to that test's own turn.
    """
    safe = "".join(ch for ch in tag if ch.isalnum())[-12:] or "x"
    return feishu_dm(f"ou_{safe}")


def fixture_assembler(clock):
    """The assembly a *configured* profile would have, as a fixture.

    A wake is only released when an assembly is installed: the released round's only allowed
    material is the assembled candidate, so a host without one is a host that cannot release anything.
    These hosts therefore install the same stand-in the shared fixtures use — one ordinary, recent,
    user-authored memory record — instead of running the real local sources against the throwaway home.
    ``clock`` is the host's clock callable: the record is always "an hour before the round", so a test
    that moves the host clock (the suite's usual way of making a window due) never makes it stale.
    """
    from proactive import content as _content

    class _ClockRelativeSource(_content.StaticSource):
        """One ordinary memory record, placed an hour before the round being decided."""

        def read(self, scope):
            self.calls += 1
            return (_content.Record(source="cross-session-memory", record_id="t-fixture",
                                    text="周五和老王吃饭",
                                    at=float(clock()) - 3600, subject="user"),)

    policy = _content.ContentPolicy(recent_record_seconds=48 * 3600)
    return _content.Assembler((_ClockRelativeSource(name="cross-session-memory"),), policy)


def session_db():
    from hermes_cli.goals import _get_session_db

    return _get_session_db()


def seed_transcript(store, db, source, rows):
    """Create the session entry and its transcript rows (the gateway does this before the hook)."""
    entry = store.get_or_create_session(source)
    db.create_session(entry.session_id, source="feishu")
    for role, content in rows:
        db.append_message(session_id=entry.session_id, role=role, content=content)
    return entry


def seed_heartbeat(session_id: str, prompt: str, *, interval_seconds: int = 60) -> None:
    """Arm a DUE heartbeat without spending the tick (``due_prompt()`` claims it on read)."""
    from hermes_cli.heartbeat import HeartbeatState, load_heartbeat, save_heartbeat

    save_heartbeat(session_id, HeartbeatState(prompt=prompt, interval_seconds=interval_seconds,
                                              created_at=1.0))
    state = load_heartbeat(session_id)
    assert state is not None and state.is_due(), (
        "fixture bug: the armed heartbeat must read back DUE for the real poll"
    )


def transcript_rows(store, session_id: str) -> List[tuple]:
    return [(str(m.get("role")), str(m.get("content"))) for m in store.load_transcript(session_id)]


def roles_alternate(rows) -> bool:
    """No two consecutive rows share a role (the alternation a delivered round must keep)."""
    roles = [role for role, _ in rows]
    return all(a != b for a, b in zip(roles, roles[1:]))


# ── the isolated host: one scenario's world ───────────────────────────────────────────────────


@dataclass
class SessionUnderTest:
    store: Any
    db: Any
    runner: Any
    adapter: Any
    source: Any
    entry: Any
    captured: Dict[str, Any] = field(default_factory=dict)


class IsolatedHost:
    """One throwaway Hermes host driving the real proactive-conversation chain end to end."""

    def __init__(self, tmp_path: Path, srv, *, isolation: str = "in_process", enabled: bool = True,
                 source=None, transcript=(("user", "上次聊到X"), ("assistant", "嗯，X我们改天再说")),
                 extra_config: str = "", extra_plugins=(), probe_log: Optional[Path] = None,
                 session_tag: Optional[str] = None, expect_refusal: bool = False,
                 material: bool = True, result_patch=None):
        self.tmp_path = Path(tmp_path)
        self.base_url = srv.base_url
        self._srv = srv
        self.isolation = isolation
        self.enabled = enabled
        #: Under ``plugins.isolation: host`` the plugin must *refuse to arm* (``register`` raises),
        #: so this harness variant expects a load failure instead of a clean registration. The default
        #: is the ordinary expectation (a plugin that loads) for every in-process scenario.
        self.expect_refusal = expect_refusal
        #: Install the fixture assembly (a configured profile's stand-in) at start. Off only for
        #: the tests that measure the *absence* of a composition (``off:content-unconfigured``).
        self.material = material
        self.source = source or unique_source(session_tag or Path(tmp_path).name)
        self.transcript_seed = list(transcript)
        self.extra_config = extra_config
        self._extra_plugins = list(extra_plugins)
        self._probe_log = Path(probe_log) if probe_log is not None else None
        #: Fault injection at the gateway's own agent seam (``bind_real_agent``): a callable that
        #: rewrites the result dict production's ``_run_agent`` would hand the gateway.
        self._result_patch = result_patch
        self._original_config: Optional[str] = None
        self._clock_now = 1_800_000_000.0  # 2027-01-15 UTC; only offsets are ever asserted
        self.store = None
        self.db = None
        self.runner = None
        self.adapter = None
        self.entry = None
        self.captured = {}
        self.loaded = None
        self.manager = None

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> "IsolatedHost":
        install_plugin_tree()  # creates the isolated config on the very first start of the session
        self._original_config = (support.CONFIG_PATH.read_text(encoding="utf-8")
                                 if support.CONFIG_PATH.exists() else None)
        if self._probe_log is not None:
            from probe_plugin import PROBE_NAME, install_probe_plugin

            install_probe_plugin(self._probe_log)
            if PROBE_NAME not in self._extra_plugins:
                self._extra_plugins.append(PROBE_NAME)
        write_home_config(self.base_url, isolation=self.isolation, enabled=self.enabled,
                          extra_plugins=self._extra_plugins, extra=self.extra_config)
        self.loaded, self.manager = discover_installed_plugin()
        if self.expect_refusal:
            # The plugin is expected to REFUSE to arm in this configuration, so the loader must
            # record a load failure, leave the runtime record disabled, and end up with no
            # registration at all (nothing that could intercept a round).
            assert self.enabled, "expect_refusal describes an enabled plugin that refuses to arm"
            assert self.loaded.error, (
                "the plugin was expected to refuse to arm here, but the loader recorded no error: "
                f"enabled={self.loaded.enabled} hooks={list(self.loaded.hooks_registered)}"
            )
            assert self.loaded.enabled is False, (
                f"a refused load must leave the plugin disabled: enabled={self.loaded.enabled}"
            )
            assert not self.loaded.hooks_registered, (
                f"a refusing plugin must register nothing, got {list(self.loaded.hooks_registered)}"
            )
        else:
            assert self.loaded.enabled is self.enabled, (
                f"plugins.enabled={self.enabled} but the loader reports enabled={self.loaded.enabled}"
            )
            if self.enabled:
                assert self.loaded.error is None, f"the plugin failed to load: {self.loaded.error}"
        self.store = support.new_store(self.tmp_path)
        self.db = session_db()
        self.runner = build_runner(self.store, self.db)
        self.adapter = build_adapter(self.runner)
        self.entry = seed_transcript(self.store, self.db, self.source, self.transcript_seed)
        self.captured = {}
        bind_real_agent(self.runner, self.db, self.entry.session_id, self.base_url, self.captured,
                        result_patch=self._result_patch)
        if self.in_process:
            self.runtime.set_clock(lambda: self._clock_now)
            self.runtime.set_rng(support.FixedRng(0.5))
            if self.material:
                # A released round needs an assembly; without one the wake is refused
                # (``off:content-unconfigured``). Tests that measure the *release* install the same
                # fixture stand-in a configured profile's ``composition`` setting would produce.
                self.runtime.set_content(fixture_assembler(lambda: self._clock_now))
        return self

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.in_process:
                self.runtime.reset_seams()
        with contextlib.suppress(Exception):
            from proactive import runtime as local_runtime

            local_runtime.reset_seams()
        shutdown_plugin_host()
        if self._original_config is not None:
            support.CONFIG_PATH.write_text(self._original_config, encoding="utf-8")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # -- the plugin under test -----------------------------------------------------------------

    @property
    def in_process(self) -> bool:
        """True when the live callbacks are this process's plugin modules (not host proxies)."""
        return hooks_are_in_process()

    @property
    def host_module(self) -> Optional[str]:
        """The module the live callback claims to live in (evidence of where the plugin loaded)."""
        callback = plugin_callback()
        return getattr(callback, "__module__", None)

    @property
    def runtime(self):
        """The runtime module the live callbacks belong to.

        In-process: the module hermes loaded (``hermes_plugins.<plugin>.proactive.runtime``) — its
        seam state and in-flight tickets are the ones the gate uses. Host mode: the plugin code
        lives in the host process, so nothing here can reach it, and the local package copy is used
        for the two operations this harness needs (seeding the per-session blob, pausing) — both are
        plain store writes and therefore byte-identical in either mode.
        """
        if self.in_process:
            return sys.modules[self.host_module]
        from proactive import runtime

        return runtime

    # -- clock ---------------------------------------------------------------------------------

    @property
    def now(self) -> float:
        return self._clock_now

    def set_clock(self, when: float) -> float:
        self._clock_now = float(when)
        if self.in_process:
            self.runtime.set_clock(lambda: self._clock_now)
        return self._clock_now

    def at(self, iso: str, tz: str = "Asia/Shanghai") -> float:
        """Point the plugin clock at a wall-clock moment in ``tz`` (fixtures speak local time)."""
        from zoneinfo import ZoneInfo

        return self.set_clock(datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(tz)).timestamp())

    def advance(self, seconds: float) -> float:
        return self.set_clock(self._clock_now + float(seconds))

    # -- per-session configuration / state -----------------------------------------------------

    def dims(self) -> Dict[str, Any]:
        return support.dims_for(self.store, self.entry, self.source)

    def enable_session(self, config_raw: Dict[str, Any]) -> Dict[str, Any]:
        """Enable this session the way the enable/control flow does (a real store write)."""
        assert self.runtime.enable_session(self.store, self.entry.session_key, self.dims(),
                                           config_raw), "the harness's session config was refused"
        return config_raw

    def raw_blob(self) -> Any:
        return self.store.get_session_metadata(self.entry.session_key, META_KEY, None)

    def blob_present(self) -> bool:
        """Whether the plugin's metadata key exists at all.

        ``raw_blob`` cannot answer this: a permanent close stores JSON ``null`` and a never-written
        key reads back as ``None`` through the same default. The sentinel read is the only way to
        tell "closed" from "absent", the distinction the store and policy rules rest on.
        """
        return self.store.get_session_metadata(self.entry.session_key, META_KEY, _ABSENT) is not _ABSENT

    def pause(self) -> bool:
        return self.runtime.pause_session(self.store, self.entry.session_key, self.dims())

    def revoke(self) -> bool:
        return self.runtime.revoke_session(self.store, self.entry.session_key)

    def new_session(self):
        """Start a NEW gateway session for the same session_key — the store's own reset seam.

        This is what a ``/new`` or an automatic reset does: the routing entry for the session_key is
        replaced, so that entry's metadata does not carry over to the new ``session_id``. The host
        switches to the new entry, so every later observation addresses the new session.
        """
        entry = self.store.get_or_create_session(self.source, force_new=True)
        assert entry.session_id != self.entry.session_id, (
            "the store replaced the entry without a new session id; this is not a reset")
        self.entry = entry
        return entry

    # -- drive the real lane -------------------------------------------------------------------

    async def poll(self, *, arm: bool = True, prompt: str = "看看X") -> None:
        """One REAL poll iteration of the heartbeat lane, drained to completion."""
        if arm:
            seed_heartbeat(self.entry.session_id, prompt)
        watch = {self.entry.session_key: (self.source, self.entry.session_id)}
        await self.runner._heartbeat_poll_once(watch)
        await self.settle()

    async def poll_watch(self, *, arm: bool = True, prompt: str = "看看X") -> dict:
        """The real poll, without draining (for tests that need the run in flight)."""
        if arm:
            seed_heartbeat(self.entry.session_id, prompt)
        watch = {self.entry.session_key: (self.source, self.entry.session_id)}
        await self.runner._heartbeat_poll_once(watch)
        return watch

    def start_poll(self, *, arm: bool = True, prompt: str = "看看X"):
        """Start the real poll as a task; the caller decides when to settle."""
        async def _run():
            return await self.poll_watch(arm=arm, prompt=prompt)

        return asyncio.create_task(_run())

    def make_user_event(self, text: str, *, message_id: str = "m-user"):
        from gateway.platforms.event import MessageEvent

        return MessageEvent(text=text, message_id=message_id, source=self.source,
                            metadata={"gateway_session_key": self.entry.session_key})

    async def send_user_message(self, text: str, *, message_id: str = "m-user") -> None:
        """A real user inbound message through the same lane (adapter → admission → turn)."""
        await self.adapter.handle_message(self.make_user_event(text, message_id=message_id))
        await self.settle()

    async def admit_user_message(self, text: str, *, message_id: str = "m-user") -> None:
        """Deliver a user message up to admission only (no drain) — for racing a live turn."""
        await self.adapter.handle_message(self.make_user_event(text, message_id=message_id))

    async def admit(self, text: str = "在吗", *, message_id: str = "m-mid-turn"):
        """Run the gateway's own admission hook for a new user message.

        ``GatewayRunner._hm_pre_gateway_dispatch_hook`` (gateway/run_inbound.py:73) is the single
        production call site of ``pre_gateway_dispatch``; this method calls it directly instead of
        going through ``adapter.handle_message``. That is deliberate and measured: a message that
        arrives while the session is busy is queued by the adapter and **never** reaches admission
        during the running turn (``gateway/platforms/base.py`` ~4186 and the comment at
        ``gateway/run_busy.py`` "a steered or queued follow-up never reaches _hm_admit_event"), so
        the adapter lane cannot represent "the session changed during the released turn". The
        dedicated lane test pins that behaviour separately.
        """
        event = self.make_user_event(text, message_id=message_id)
        return await self.runner._hm_pre_gateway_dispatch_hook(event, self.source)

    async def settle(self, *, quiet_rounds: int = 3, max_rounds: int = 900) -> None:
        """Let the adapter's real background tasks (the turn, any queued turn) finish."""
        quiet = 0
        for _ in range(max_rounds):
            pending = list(getattr(self.adapter, "_background_tasks", ()) or ())
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                quiet = 0
            else:
                quiet += 1
                if quiet >= quiet_rounds and not getattr(self.adapter, "_active_sessions", None):
                    return
            await asyncio.sleep(0.02)

    async def wait_for_ticket(self, *, rounds: int = 400) -> bool:
        """Wait until the gate released this session (the released turn is now running)."""
        for _ in range(rounds):
            if self.entry.session_id in self.runtime.tickets():
                return True
            await asyncio.sleep(0.01)
        return False

    async def wait_for_turns(self, count: int, *, rounds: int = 600) -> bool:
        """Wait until ``count`` real turns were started on the bound agent seam."""
        for _ in range(rounds):
            if len(self.captured.get("turns", [])) >= count:
                return True
            await asyncio.sleep(0.02)
        return False

    # -- observations --------------------------------------------------------------------------

    @property
    def wire(self) -> List[str]:
        return [str(item["content"]) for item in self.adapter.wire]

    def delivered_containing(self, needle: str) -> List[str]:
        return [text for text in self.wire if needle in text]

    def rows(self) -> List[tuple]:
        return transcript_rows(self.store, self.entry.session_id)

    def contents(self) -> List[str]:
        return [content for _, content in self.rows()]

    def llm_calls(self) -> Dict[str, int]:
        posts = [r for r in self.requests() if str(r["path"]).rstrip("/").endswith("/chat/completions")]
        return {
            "main": sum(1 for r in posts if r.get("kind") == "main"),
            "aux": sum(1 for r in posts if r.get("kind") == "aux"),
            "post_total": len(posts),
            "all_requests": len(self.requests()),
        }

    def requests(self) -> List[Dict[str, Any]]:
        return list(getattr(self._srv, "requests", []) or [])

    def turns(self) -> List[Any]:
        return list(self.captured.get("turns", []))


P2_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 3600,
    "max_idle_seconds": 3600,
    "window_max_attempts": 4,
    "window_min_visible": 1,
    "day_max_silent": 4,
    "day_max_attempts": 6,
}
