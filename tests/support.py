"""Shared test helpers (fixtures live in ``conftest.py``).

Importing this module pins an isolated ``HERMES_HOME`` *before* any hermes module is imported, so
no test can read or write the live profile: every session store, config lookup and plugin
discovery in this suite resolves under a throwaway directory.
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# Test harness: never trigger the host's self-update / lazy-install relaunch from a test process.
# A self-managed Hermes install (the checkout the tests import) re-execs the interpreter through
# ``hermes_cli.venv_sync.prepare_launch`` to sync dependencies for the *current* ``HERMES_HOME``.
# Under this suite ``HERMES_HOME`` is a throwaway directory, so that relaunch can never complete
# (and, on a Python install without pytest, fails with ``RelaunchExit``), turning ordinary unit
# tests into environment failures. The dependency set is already present; this is the documented
# bypass, and it is set here — before any hermes module is imported — so it also propagates to the
# host subprocesses some tests spawn.
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")

PKG_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR_NAME = "proactive-conversation"

#: Throwaway hermes home for the whole test session (never the live profile).
#: ``PCHAT_ISO_HOME`` lets a *child process* spawned by a test join the same throwaway home instead
#: of making its own — a test that needs a genuinely restarted process reading the same durable
#: store can join it. It is unset in every ordinary run, so the
#: home is still created fresh per session; the value is only ever a throwaway temp dir.
ISO_HOME = Path(os.environ.get("PCHAT_ISO_HOME") or tempfile.mkdtemp(prefix="pchat-home-"))
os.environ["HERMES_HOME"] = str(ISO_HOME)
os.environ["HERMES_BUNDLED_PLUGINS"] = str(ISO_HOME / "no-bundled-plugins")

if str(PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(PKG_ROOT))

# ── configuration fixtures ────────────────────────────────────────────────────────────

CONFIG_A: Dict[str, Any] = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "dnd_start": "23:00",
    "dnd_end": "09:00",
    "min_idle_seconds": 6 * 3600,
    "max_idle_seconds": 24 * 3600,
    "window_max_attempts": 6,
    "window_min_visible": 1,
    "day_max_silent": 2,
    "day_max_attempts": 2,
}

CONFIG_B: Dict[str, Any] = {
    "enabled": True,
    "timezone": "Asia/Tokyo",
    "dnd_start": "21:00",
    "dnd_end": "07:00",
    "min_idle_seconds": 3600,
    "max_idle_seconds": 3 * 3600,
    "window_max_attempts": 2,
    "window_min_visible": 1,
    "day_max_silent": 1,
    "day_max_attempts": 1,
}


def config(raw: Dict[str, Any]):
    """Parse a raw config, asserting it is usable (test fixtures must be valid)."""
    from proactive import config as _config

    parsed = _config.parse(raw)
    assert parsed.ok, f"fixture config is not usable: {parsed.reason} {parsed.field}"
    return parsed.config


def epoch(tz_name: str, year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    """Epoch seconds of a wall-clock moment in ``tz_name`` (fixtures speak local time)."""
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz_name)).timestamp()


def local(y: int, m: int, d: int, hh: int = 0, mm: int = 0, tz: str = "UTC") -> float:
    """Epoch seconds for a UTC (default) wall-clock moment — no session timezone involved."""
    return epoch(tz, y, m, d, hh, mm)


def at_clock(when: float) -> "datetime":
    """A naive host-local ``datetime`` for an epoch — the shape ``MessageEvent`` builds itself."""
    from datetime import datetime

    return datetime.fromtimestamp(float(when))


class FixedRng:
    """Returns a fixed fraction of the window: ~``value`` in ``[0, 1]`` → deterministic deadline."""

    def __init__(self, fraction: float) -> None:
        self.fraction = fraction
        self.calls = 0

    def uniform(self, a: float, b: float) -> float:
        self.calls += 1
        return float(a) + (float(b) - float(a)) * self.fraction


def seeded_rng(seed: int = 1234) -> random.Random:
    return random.Random(seed)


# ── gateway objects ─────────────────────────────────────────────────────────────────────────


def dm_source(chat_id: str = "42", profile: Optional[str] = None):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, user_id=chat_id,
                         chat_type="dm", profile=profile)


def group_source(chat_id: str = "-100", thread_id: Optional[str] = None, profile: Optional[str] = None):
    from gateway.config import Platform
    from gateway.session import SessionSource

    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, user_id="7",
                         chat_type="group", thread_id=thread_id, profile=profile)


def make_event(
    *,
    text: str = "你好",
    source: Any = None,
    session_key: Optional[str] = None,
    heartbeat_session_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    internal: bool = False,
):
    """A real ``MessageEvent`` shaped the way the gateway hands it to the dispatch hook."""
    from gateway.platforms.event import MessageEvent

    event = MessageEvent(text=text, message_id="m1", source=source or dm_source())
    event.internal = internal
    if session_key:
        event.metadata["gateway_session_key"] = session_key
    if heartbeat_session_id:
        event._heartbeat_session_id = heartbeat_session_id
    if timestamp is not None:
        event.timestamp = timestamp
    return event


def make_wake(session_key: str, session_id: str, *, text: str = "[Heartbeat — recurring instruction]",
              source: Any = None, timestamp: Optional[datetime] = None):
    """A heartbeat wake as the poller builds it: non-internal, tagged with the heartbeat session id."""
    src = source or dm_source()
    return make_event(text=text, source=src, session_key=session_key,
                      heartbeat_session_id=session_id, timestamp=timestamp)


def new_store(tmp_path, name: str = "gateway_sessions"):
    """A real ``SessionStore`` over a throwaway directory (real journal, real metadata)."""
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    return SessionStore(sessions_dir=Path(tmp_path) / name, config=GatewayConfig())


def seed_session(store, source):
    """Create the store entry for a source (the gateway does this before the hook runs)."""
    return store.get_or_create_session(source)


def dims_for(store, entry, source) -> Dict[str, Any]:
    """The dimension fingerprint the runtime would compute for this event."""
    from proactive import store as _store

    platform = getattr(source, "platform", None)
    return _store.dims_for(
        session_key=entry.session_key,
        session_id=entry.session_id,
        profile=getattr(source, "profile", None),
        platform=getattr(platform, "value", None) or (str(platform) if platform else None),
        chat_id=getattr(source, "chat_id", None),
        thread_id=getattr(source, "thread_id", None),
    )


def install_plugin() -> Path:
    """Copy this package into ``<ISO_HOME>/plugins/proactive-conversation`` exactly once per session."""
    target = ISO_HOME / "plugins" / PLUGIN_DIR_NAME
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PKG_ROOT, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".pytest_cache"))
    CONFIG_PATH.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return target


# ── plugin-level settings: the isolated home's config.yaml ───────────────────────────────────

#: The isolated profile's config file. Plugin-level settings live here
#: (``plugins.entries.proactive-conversation.settings.*``), which is exactly what ``ctx.get_config`` reads.
CONFIG_PATH = ISO_HOME / "config.yaml"
DEFAULT_CONFIG_TEXT = "plugins:\n  enabled: [proactive-conversation]\n"


def _yaml_scalar(value: Any) -> str:
    """One YAML scalar as a user would write it (floats keep their point, strings get quoted)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"the isolated config writer emits scalars only, not {type(value).__name__}")


def _yaml_field(key: str, value: Any, indent: int) -> list:
    """One ``key: value`` field, as YAML lines — scalars, or a list of mappings (session declarations)."""
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        lines = [f"{pad}{key}:"]
        for item in value:
            if not isinstance(item, dict) or not item:
                raise TypeError("the isolated config writer emits a list of non-empty mappings only")
            for position, (item_key, item_value) in enumerate(item.items()):
                prefix = f"{pad}  - " if position == 0 else f"{pad}    "
                lines.append(f"{prefix}{item_key}: {_yaml_scalar(item_value)}")
        return lines
    return [f"{pad}{key}: {_yaml_scalar(value)}"]


def write_plugin_settings(values: Optional[Dict[str, Any]] = None) -> Path:
    """Write ``plugins.entries.proactive-conversation.settings`` in the isolated config.yaml.

    ``None`` / ``{}`` clears the subtree — the shape of a profile where the user never set
    anything. Hermes' own loader reads this file, so the plugin's settings path is exercised for
    real; nothing outside the throwaway home is touched. A list value (the
    ``session_declarations``) is written as a YAML list of mappings.
    """
    lines = ["plugins:", f"  enabled: [{PLUGIN_DIR_NAME}]"]
    if values:
        lines += ["  entries:", f"    {PLUGIN_DIR_NAME}:", "      settings:"]
        for key, value in values.items():
            lines += _yaml_field(key, value, indent=8)
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return CONFIG_PATH


def config_text() -> str:
    """The isolated config.yaml as text (so a test can restore it verbatim)."""
    return CONFIG_PATH.read_text(encoding="utf-8")


def load_plugin():
    """Discover and load the installed plugin with hermes' own loader; assert it is live."""
    import hermes_cli.plugins as plugins_mod

    plugins_mod._reset_plugin_managers_for_tests()
    plugins_mod.discover_plugins()
    manager = plugins_mod.get_plugin_manager()
    loaded = [p for p in manager._plugins.values() if p.manifest.name == PLUGIN_DIR_NAME]
    assert loaded and loaded[0].enabled and loaded[0].error is None, (
        "the plugin did not load from the isolated home: "
        f"{[(p.manifest.name, p.enabled, p.error) for p in manager._plugins.values()]}"
    )
    for hook in ("pre_gateway_dispatch", "transform_llm_output"):
        assert plugins_mod.has_hook(hook), f"{hook} has no live subscriber"
    return loaded[0]


def loaded_runtime():
    """The ``runtime`` module the *registered* callbacks live in (the plugin's own copy).

    The plugin is loaded as ``hermes_plugins.<name>.proactive.runtime``, which is a different module
    object from a plain ``import proactive.runtime`` in the test process. The seams and the in-flight
    tickets are module state, so tests that drive the real hooks must use this object.
    """
    import hermes_cli.plugins as plugins_mod

    manager = plugins_mod.get_plugin_manager()
    callbacks = manager._hooks.get("pre_gateway_dispatch") or []
    assert callbacks, "the plugin is not loaded, so it has no runtime module"
    return sys.modules[callbacks[0].__module__]


def unload_plugins() -> None:
    """Leave the process-wide plugin registry exactly as found."""
    import hermes_cli.plugins as plugins_mod

    plugins_mod._reset_plugin_managers_for_tests()


def invoke_hook(name: str, **kwargs) -> list:
    """Invoke a hook through hermes' own delivery manager (the production invocation path)."""
    import hermes_cli.plugins as plugins_mod

    return plugins_mod.invoke_hook(name, **kwargs)


#: The one directive a released (material-carrying) round adds to its hook result.
RELEASE_DIRECTIVE = [{"action": "allow"}]


def released(directives) -> list:
    """Normalise the release directive for harnesses whose question is "did the wake release?".

    A released round also carries the assembled material, so the plugin writes it in place
    and answers ``{"action": "allow"}`` — the gateway's documented "dispatch normally" plus its
    first-action-wins stop (``gateway/run_inbound.py``) — instead of ``None``. A harness that
    documents ``[]`` as "released, nothing to say" accepts exactly that one directive **and checks
    it**: a ``skip``, a ``rewrite``, a second directive, or ``allow`` alongside anything else comes
    back untouched, so a refusal or an unexpected verdict is never silently swallowed. The material
    write itself (``event.text == material`` on the very same event object) is asserted end to end
    in the host-level composition tests; these in-memory harnesses measure the
    gate/ticket layer, not the assembly.
    """
    return [] if directives == RELEASE_DIRECTIVE else directives


def reload_modules() -> None:
    """Drop cached copies of the plugin subpackage so a fresh import sees the delivered files."""
    for name in list(sys.modules):
        if name == "proactive" or name.startswith("proactive."):
            del sys.modules[name]
