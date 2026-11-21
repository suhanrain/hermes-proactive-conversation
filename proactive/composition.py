"""The composition root for the read-only sources: real adapters → one assembler.

Where this fits
---------------
``proactive.content`` is deliberately source-agnostic: it knows a whitelist, a registry, a policy and
a set of template rules, and it reads whatever adapters are handed to it. Nothing in it may reach
for a database. This module is the other half: it *builds* the real adapters for the two local
sources, derives the profile they belong to from the profile's own home directory, and hands the
result to :func:`proactive.runtime.set_content` — the one seam that turns an assembled candidate into
a released round's material.

It is installed **only from inside the plugin process**. Two consequences make this safe:

* the composition decides nothing about a session: the wake path reaches the assembly *after*
  ``policy.evaluate_release`` said "this session may open a turn", so a session that is off,
  paused, in DND, over its caps or outside its window never causes a read;
* nothing here is registered with Hermes — no hook, no tool, no gateway change. ``install()``
  installs into the plugin's own runtime module, and ``uninstall()`` (or
  ``runtime.reset_seams()``) takes it out again.

Configuration: values, never constants
--------------------------------------
Every time and every count is a field of :class:`CompositionConfig`, and the windows have **no
defaults**: ``build()`` refuses an incomplete config instead of guessing (the same rule
``ContentPolicy.recent_record_seconds`` already follows — 时间次数不得写死). :data:`PM_RECOMMENDED`
records the values the PM chose (最近 50 条 或 30 天内（取小）；记忆最近 30 天), and it is a *value set a
caller may pass explicitly*: nothing in this module applies it implicitly, so no code path can
silently inherit a window the user never confirmed.

The two topic-rule tables are the exception that proves the rule, and they are still values:
``transcript_topic_rules`` / ``memory_topic_rules`` default to the two named tables the sources ship
with (``TRANSCRIPT_TOPIC_RULES`` — machine text out, a 6-character floor and
the pure-acknowledgement table in; ``MEMORY_TOPIC_RULES`` — machine text only, because a memory
entry is curated prose). A rule table has no per-session meaning the way a window does, so a
documented default is the honest shape; a caller can still pass any table, including an empty one,
and ``Composition.wiring()`` prints the effective tables so a report never has to guess.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from . import content
from . import contract as C
from . import host_adapter as _adapter
from . import locales as _locales
from .sources import (MEMORY_TOPIC_RULES, TRANSCRIPT_TOPIC_RULES, CrossSessionMemorySource,
                      SessionTranscriptSource, TopicRules, topic_rules_from_profile)

logger = logging.getLogger(__name__)

DAY = 24 * 3600.0


#: ``resolve_home`` provenance: the home was derived from the directory this plugin instance was
#: loaded from. The only evidence a multiplexing process leaves behind.
HOME_FROM_PLUGIN_INSTANCE = "plugin-instance"
#: ``resolve_home`` provenance: no plugin identity was available (a bare capability probe, a caller
#: that passed no context), so Hermes' own process/scope resolution answered.
HOME_FROM_PROCESS = "process-home"

#: The directory name Hermes uses for both its bundled and its per-home plugin roots
#: (``hermes_cli/plugins_discovery.py``: ``user_dir = get_hermes_home() / "plugins"``).
_PLUGIN_ROOT_NAME = "plugins"

#: Manifest sources whose plugin directory is rooted at a **home** (Hermes' own discovery rule
#: above). ``bundled`` lives in the install tree (``hermes-agent/plugins``), ``entrypoint`` plugins
#: have no directory at all, and ``project`` is ``Path.cwd()/.hermes/plugins`` — a *workspace* that
#: reproduces the ``<home>/plugins`` shape without being anybody's home (``collect_directory_manifests``
#: in ``hermes_cli/plugins_discovery.py``, guarded by ``HERMES_ENABLE_PROJECT_PLUGINS``), so none of
#: the three may be turned into a data root. Only ``user`` — the per-home ``<home>/plugins`` root — is.
_HOME_ROOTED_SOURCES = ("user",)


@dataclass(frozen=True)
class CompositionConfig:
    """The composition's knob set. Every field is required.

    ``recent_record_seconds`` — what "recent" means for the assembly itself: a record older than
    this is not a topic. Set it to at least the widest source window: a narrower value
    silently tightens the sources' own windows, which is legal but is logged as such.

    ``transcript_max_messages`` / ``transcript_window_seconds`` — the transcript window: at most this
    many of this session's newest messages, none older than this many seconds (the effective window
    is the smaller of the two). The count is not only a number: the adapter binds it as a real SQL
    parameter (``LIMIT ?``), so it must also fit the database's integer domain
    (:data:`SQLITE_INT_MAX`) — a count the driver cannot bind is refused here rather than failing
    inside the read, after the assembly has already been installed.

    ``memory_window_seconds`` — how old a memory *file* may be and still count as this profile's
    recent memory (memory entries carry no timestamp of their own; see
    :mod:`proactive.sources.memory`).

    ``transcript_topic_rules`` / ``memory_topic_rules`` — which rows the two sources may offer as a
    topic at all (:class:`proactive.sources.TopicRules`). They are fields here for the same reason
    every window is: the rule table is a value, not a literal. The defaults are the two named tables
    the sources ship with — ``TRANSCRIPT_TOPIC_RULES`` (machine text + the 6-character floor + the
    pure-acknowledgement table) and ``MEMORY_TOPIC_RULES`` (machine text only: a memory entry is
    curated prose, and the floor would drop a good short one like 「周五面试」)
    — and a caller may pass any other table, including an empty one.
    """

    recent_record_seconds: Any
    transcript_max_messages: Any
    transcript_window_seconds: Any
    memory_window_seconds: Any
    transcript_topic_rules: TopicRules = TRANSCRIPT_TOPIC_RULES
    memory_topic_rules: TopicRules = MEMORY_TOPIC_RULES

    def problems(self) -> Tuple[str, ...]:
        """Every reason this config cannot be used, as field names (empty tuple = usable).

        A validation function must never raise: the values come straight from ``config.yaml``, so an
        operator can write ``.inf``, ``.nan`` or an integer too large for a float (``10**400``). Each
        of the four required numbers is therefore screened by :func:`_as_finite_positive`, which
        answers ``None`` for anything that is not a positive, finite, safely computable number — the
        field is then named as a problem instead of making this call raise.

        ``transcript_max_messages`` is screened one step further: it is a *count a query binds*, not
        a window this module computes with, so :func:`_as_bindable_count` also refuses a value the
        database cannot accept as an integer parameter. That failure only shows up
        inside the read, i.e. after a config that looked usable had already installed an assembly.
        """
        bad = []
        if _as_finite_positive(self.recent_record_seconds) is None:
            bad.append("recent_record_seconds")
        if _as_bindable_count(self.transcript_max_messages) is None:
            bad.append("transcript_max_messages")
        if _as_finite_positive(self.transcript_window_seconds) is None:
            bad.append("transcript_window_seconds")
        if _as_finite_positive(self.memory_window_seconds) is None:
            bad.append("memory_window_seconds")
        for field, rules in (("transcript_topic_rules", self.transcript_topic_rules),
                             ("memory_topic_rules", self.memory_topic_rules)):
            if not isinstance(rules, TopicRules):
                bad.append(field)
        return tuple(bad)

    @property
    def usable(self) -> bool:
        return not self.problems()

    def widest_source_window(self) -> float:
        """The widest window any source is configured with (used for the narrowing warning)."""
        values = []
        for value in (self.transcript_window_seconds, self.memory_window_seconds):
            if _is_positive_number(value):
                values.append(float(value))
        return max(values) if values else 0.0


#: The recommended values. **Advisory**: pass them explicitly to :func:`build` if you want
#: them. Nothing here applies them on its own, so no install can inherit a window by accident.
PM_RECOMMENDED = CompositionConfig(
    recent_record_seconds=30 * DAY,
    transcript_max_messages=50,
    transcript_window_seconds=30 * DAY,
    memory_window_seconds=30 * DAY,
)

#: The same values in the shape the ``composition`` setting is written in. Still a
#: value set, not a default: :func:`try_from_settings` never falls back to it, and a profile that
#: does not carry this block installs nothing and releases no wake.
PM_RECOMMENDED_SETTINGS: Dict[str, Any] = {
    "recent_record_seconds": 30 * DAY,
    "transcript_max_messages": 50,
    "transcript_window_seconds": 30 * DAY,
    "memory_window_seconds": 30 * DAY,
}


class CompositionIncomplete(ValueError):
    """Raised when a config is missing an unusable value: wiring fails loudly, never silently."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("incomplete composition configuration: " + ", ".join(problems))
        self.problems = tuple(problems)


# ── the configured composition: settings value → CompositionConfig ──────────────────────────
# The value lives in ``plugins.entries.proactive-conversation.settings.composition``
# (``contract.SETTING_COMPOSITION``) and is read through ``ctx.get_config`` on every hook call, so an
# edit applies to the next round without a reload. Nothing here applies a default window: a missing
# required field is a *problem*, and a problem means the plugin refuses to install the composition
# and refuses to release a wake (``off:content-unconfigured``) — a released round without an assembly
# would be a round whose material is the raw heartbeat prompt.

#: The fields every configured composition must carry (时间次数不得写死: no built-in default).
REQUIRED_SETTING_FIELDS = (
    "recent_record_seconds",
    "transcript_max_messages",
    "transcript_window_seconds",
    "memory_window_seconds",
)

#: The optional fields. The two rule tables default to the tables the sources ship with (see
#: :class:`CompositionConfig`).
OPTIONAL_SETTING_FIELDS = ("transcript_topic_rules", "memory_topic_rules")

#: The fields a rule table may carry (each one a value; see :class:`proactive.sources.TopicRules`).
RULE_TABLE_FIELDS = ("machine_markers", "min_chars", "min_words", "acknowledgement_words",
                     "exclude_compaction_summaries")

#: Every field name a configured composition may carry.
SETTING_FIELDS = REQUIRED_SETTING_FIELDS + OPTIONAL_SETTING_FIELDS


def _rule_table(raw: Any, field: str, default: TopicRules,
                problems: list) -> TopicRules:
    """One topic-rule table from configuration; ``default`` when the field is absent.

    A table is a mapping of the four :data:`RULE_TABLE_FIELDS`; anything else — a list, a string, an
    unknown key, a value the dataclass refuses — is a problem, never a silently ignored typo.
    """
    if raw is None:
        return default
    if not isinstance(raw, dict):
        problems.append(field)
        return default
    unknown = sorted(set(raw) - set(RULE_TABLE_FIELDS))
    if unknown:
        problems.append(f"{field}.{unknown[0]}")
    kwargs: Dict[str, Any] = {}
    for key in RULE_TABLE_FIELDS:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if key in ("machine_markers", "acknowledgement_words"):
            if not isinstance(value, (list, tuple)) or not all(
                    isinstance(item, str) and item for item in value):
                problems.append(f"{field}.{key}")
                continue
            kwargs[key] = tuple(value)
        elif key in ("min_chars", "min_words"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"{field}.{key}")
                continue
            kwargs[key] = value
        else:  # exclude_compaction_summaries
            if not isinstance(value, bool):
                problems.append(f"{field}.{key}")
                continue
            kwargs[key] = value
    try:
        return TopicRules(**kwargs)
    except Exception:
        problems.append(field)
        return default


def from_settings(raw: Any) -> CompositionConfig:
    """Parse the ``composition`` setting value into a :class:`CompositionConfig`.

    Raises :class:`CompositionIncomplete` (with the offending field names, never their values) when
    the value is not a mapping, names a field this version does not know, or carries a missing or
    unusable value. Never reads anything and never raises anything else.
    """
    config, problems = try_from_settings(raw)
    if config is None:
        raise CompositionIncomplete(problems)
    return config


def try_from_settings(raw: Any) -> Tuple[Optional[CompositionConfig], Tuple[str, ...]]:
    """``(config, problems)`` — ``(None, problems)`` exactly when nothing may be installed."""
    problems: list = []
    if not isinstance(raw, dict):
        return None, ("settings",)
    unknown = sorted(set(raw) - set(SETTING_FIELDS))
    if unknown:
        problems.append(unknown[0])
    missing = [field for field in REQUIRED_SETTING_FIELDS if field not in raw or raw[field] is None]
    problems.extend(missing)
    if problems:
        return None, tuple(dict.fromkeys(problems))
    transcript_rules = _rule_table(raw.get("transcript_topic_rules"), "transcript_topic_rules",
                                   TRANSCRIPT_TOPIC_RULES, problems)
    memory_rules = _rule_table(raw.get("memory_topic_rules"), "memory_topic_rules",
                               MEMORY_TOPIC_RULES, problems)
    config = CompositionConfig(
        recent_record_seconds=raw["recent_record_seconds"],
        transcript_max_messages=raw["transcript_max_messages"],
        transcript_window_seconds=raw["transcript_window_seconds"],
        memory_window_seconds=raw["memory_window_seconds"],
        transcript_topic_rules=transcript_rules,
        memory_topic_rules=memory_rules,
    )
    problems.extend(config.problems())  # the dataclass validates the numbers themselves
    if problems:
        return None, tuple(dict.fromkeys(problems))
    return config, ()


def settings_fingerprint(raw: Any) -> str:
    """A stable, comparable form of one configured value (change detection, never logged).

    The plugin rebuilds the composition only when this string changes, so an unchanged setting on
    every hook call costs one config read and nothing else (禁止每次 hook 重建).
    """
    if isinstance(raw, dict):
        return "mapping:" + ",".join(f"{key}={settings_fingerprint(raw[key])}"
                                     for key in sorted(raw, key=str))
    if isinstance(raw, (list, tuple)):
        return "list:[" + ",".join(settings_fingerprint(item) for item in raw) + "]"
    return f"{type(raw).__name__}:{raw!r}"


@dataclass(frozen=True)
class Composition:
    """One wired composition: the assembler, the adapters behind it, and where it reads from.

    Handed back by :func:`build` so a caller (and the delivery report) can print the wiring —
    window values, the database path, the memory directory, the profile — without reaching into
    private attributes.
    """

    assembler: content.Assembler
    sources: Tuple[Any, ...]
    policy: content.ContentPolicy
    home: Path
    profile: Optional[str] = None
    #: Which evidence produced ``home`` (see :func:`resolve_home`). Printed by the install log so the
    #: live gateway's data root is decidable from one line instead of being guessed.
    home_source: str = HOME_FROM_PROCESS
    #: The resolved locale code this composition was built with (``zh-Hans`` / ``en`` / ``neutral``),
    #: or ``None`` when built directly without a :class:`proactive.locales.LocaleActivation`.
    locale: Optional[str] = None

    @property
    def transcript(self) -> Optional[SessionTranscriptSource]:
        return next((s for s in self.sources if isinstance(s, SessionTranscriptSource)), None)

    @property
    def memory(self) -> Optional[CrossSessionMemorySource]:
        return next((s for s in self.sources if isinstance(s, CrossSessionMemorySource)), None)

    def wiring(self) -> Dict[str, Any]:
        """The composition's configuration as data (no record text, no credentials)."""
        return {
            "home": str(self.home),
            "profile": self.profile,
            "home_source": self.home_source,
            "locale": self.locale,
            "db_path": str(self.transcript.db_path) if self.transcript else None,
            "memory_dir": str(self.memory.memory_dir) if self.memory else None,
            "policy": {
                "recent_record_seconds": self.policy.recent_record_seconds,
            },
            "sources": [name for name, _ in _named(self.sources)],
            "windows": {
                "transcript": self.transcript.windows() if self.transcript else None,
                "memory": self.memory.windows() if self.memory else None,
            },
            "topic_rules": {
                "transcript": self.transcript.rules_dict() if self.transcript else None,
                "memory": self.memory.rules_dict() if self.memory else None,
            },
        }


def default_home() -> Path:
    """The home the **process** is anchored on: ``HERMES_HOME``, else Hermes' own resolution.

    This is the *process* view, and in a multiplexing gateway it is the wrong answer for a plugin
    that was loaded from one profile's plugin directory: the process serves every profile, so
    ``os.environ["HERMES_HOME"]`` is the global home (``~/.hermes``) while the plugin instance
    belongs to ``<global>/profiles/<name>``: the assembly would then read the global
    ``state.db``/``memories``, both sources would answer ``unavailable:session-scope-mismatch``,
    and every wake would stay silent as ``silent:source-down``.

    The host lookup itself lives in :func:`proactive.host_adapter.process_home`: this module keeps
    only the last resort, ``~/.hermes``, so it names no
    environment variable and imports no host module. ``None`` from the seam means "no host answered",
    which is also what a bare interpreter (the capability probe) sees.
    """
    home = _adapter.process_home()
    return home if home is not None else Path.home() / ".hermes"


#: ``resolve_home`` provenance: the home was derived from the directory this plugin instance was
#: loaded from. The only evidence a multiplexing process leaves behind.


def _manifest_leaf_name(name: Any) -> str:
    """The directory name this plugin's manifest implies, never empty.

    Hermes keys a plugin by its directory name (``manifest_key``: ``key or name``), so the manifest's
    own name is the name its directory must carry. A context without a name (a recording context, a
    hand-built one) falls back to this package's own plugin name — the constant in ``contract``, not a
    filesystem guess, because the directory this module was loaded from is the *repository* in a dev
    checkout rather than the plugin directory.
    """
    try:
        leaf = str(name or "").strip()
    except Exception:  # noqa: BLE001 - a hostile __str__ names nothing
        leaf = ""
    return leaf or C.PLUGIN_NAME


def home_for_plugin_dir(plugin_dir: Any, name: Any = None) -> Optional[Path]:
    """The Hermes home a plugin directory belongs to, or ``None`` when it cannot be proven.

    A user plugin is discovered at ``<home>/plugins/<key>`` — Hermes' own rule, not a convention this
    plugin invented — so the parent of the first ``plugins`` segment *is* that home. Category plugins
    (``<home>/plugins/<category>/<name>``) resolve the same way.

    Everything that only *looks* like that shape is refused, because the answer becomes a data root
    (another profile's ``state.db`` and ``memories``) and a wrong answer is a cross-profile read:

    * a **relative** path — ``plugins/<key>`` would otherwise resolve a home of ``.``, i.e. whatever
      directory the process happens to stand in;
    * a **traversal** (``..`` anywhere) — ``<x>/plugins/../../plugins/<key>`` keeps the ``plugins``
      segment yet names a home nothing has verified;
    * **no ``plugins`` segment** — a hand-built fixture, a symlink farm, an install tree;
    * a **leaf that is not this plugin's own name** — ``<home>/plugins`` itself, or a directory named
      after another plugin: the manifest in hand is this plugin's, so a directory carrying a
      different name is not the place it was loaded from;
    * anything unprintable as a path (a hostile/broken path-like).
    """
    if isinstance(plugin_dir, (bytes, bytearray)) or plugin_dir is None:
        return None
    try:
        raw = str(plugin_dir)
    except Exception:  # a hostile/broken path-like: no evidence, no home
        return None
    if not raw.strip() or "\x00" in raw:
        return None
    try:
        path = Path(raw)
    except Exception:  # noqa: BLE001 - a path that cannot even be built names no home
        return None
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    leaf = _manifest_leaf_name(name)
    if path.name != leaf:
        return None
    for candidate in (path, *path.parents):
        if candidate.name == _PLUGIN_ROOT_NAME and candidate.parent != candidate:
            return candidate.parent
    return None


def instance_home() -> Optional[Path]:
    """The home this plugin **instance** was loaded from, when its manifest can prove it.

    The manifest is read through the host adapter — the one place allowed to touch
    ``ctx.manifest`` — so this function takes no context: the adapter already owns it.
    ``PluginManifest.path`` is the plugin's own directory (set by
    ``scan_directory``/``parse_manifest_file`` for every directory plugin), so this needs no hermes
    import and no filesystem guess. Only ``source=user`` manifests are home-rooted: ``bundled`` and
    ``entrypoint`` live in the install tree, and ``project`` is ``Path.cwd()/.hermes/plugins`` — a
    workspace that reproduces the ``<home>/plugins`` shape without being a home (no profile has its
    ``state.db`` or ``memories`` there). A manifest with no source at all is "no evidence" too.
    """
    source = _adapter.manifest_source()
    if source not in _HOME_ROOTED_SOURCES:
        return None
    return home_for_plugin_dir(_adapter.manifest_path(), _adapter.manifest_name())


def resolve_home() -> Tuple[Path, str]:
    """``(home, source)`` — the data root for this plugin instance, and the evidence behind it.

    Takes no context: the adapter owns the context, so the instance's manifest is read there.
    Order, and why:

    1. **the plugin instance's own directory** (the manifest's ``path`` → ``<home>/plugins/<key>``).
       This is the strongest evidence available inside a hook: it is a fact about *this* loaded copy,
       it does not change between rounds, and in a multiplexing gateway it names the profile the copy
       was installed into, while the process environment names the global home for every profile.
    2. :func:`default_home` — the process view (the adapter, then ``~/.hermes``). Used only when no
       plugin identity could be read at all: a bare-interpreter capability probe, ``plugins doctor``,
       a test that builds a composition directly. It must never win over (1), and it cannot be the
       multiplexing gateway's answer, because that process loads its plugins
       through a manifest that carries a path.

    **``HOME_FROM_PROCESS`` means "no identity", not "use this home".** In a process that serves
    several profiles — every gateway — the process home belongs to *some* profile and the round being
    decided belongs to another, so a data root resolved this way may not release anything: the caller
    (:func:`proactive.runtime.refresh_content`) refuses instead of installing a composition over it.
    The process home is still *returned* because a bare interpreter has no other answer to give (the
    capability probe, ``plugins doctor``, a report), and because a caller that wants to *print* which
    root it would have used needs to see it.
    """
    home = instance_home()
    if home is not None:
        return home, HOME_FROM_PLUGIN_INSTANCE
    return default_home(), HOME_FROM_PROCESS


def identity_proven(home_source: Any) -> bool:
    """Whether a :func:`resolve_home` provenance is evidence of *this instance's* home.

    One predicate, because "the fallback answered" and "the instance answered" must never be confused
    at a release point: ``HOME_FROM_PROCESS`` is the absence of evidence.
    """
    return str(home_source or "") == HOME_FROM_PLUGIN_INSTANCE


def profile_for_home(home: Any) -> str:
    """The profile name a home directory belongs to, by Hermes' own directory convention.

    ``<anywhere>/profiles/<name>`` is profile ``<name>``; anything else — a bare ``~/.hermes``, a
    throwaway home in a test — is Hermes' default profile. This is how the adapters learn which
    profile's data they are bound to without reading any configuration.
    """
    path = Path(home)
    return path.name if path.parent.name == "profiles" else "default"


def build(config: CompositionConfig, *, home: Any = None, profile: Optional[str] = None,
          clock: Callable[[], float] = time.time, toolbox: Any = None,
          injected_tools: Sequence[Any] = (), locale_activation: Any = None) -> Composition:
    """Wire the real read-only sources for one profile home into one assembler.

    ``home`` is resolved through :func:`resolve_home` when it is not given: the plugin instance's own
    directory first, the process view only as the bare-interpreter fallback. A caller that passes
    ``home`` explicitly still wins — a test knows its own fixture home. No context argument: the
    adapter owns it.

    Raises :class:`CompositionIncomplete` when the config is unusable — the caller must have decided
    every window before anything can be read; there is no "assume 30 days" path here.
    """
    if not isinstance(config, CompositionConfig):
        raise TypeError("build() takes a CompositionConfig")
    problems = config.problems()
    if problems:
        raise CompositionIncomplete(problems)

    if locale_activation is not None:
        # The active profile decides the language rules (neutral ⇒ empty patterns / no floor / no
        # acknowledgement table — only the global machine markers + structural filter remain).
        loc_profile = locale_activation.profile
        transcript_rules = topic_rules_from_profile(loc_profile)
        memory_rules = topic_rules_from_profile(loc_profile, memory=True)
        expression_patterns: Any = _locales.expression_resources(loc_profile)
        locale_code: Any = locale_activation.code
    else:
        # No activation supplied (a direct build with an explicit CompositionConfig): use the
        # config's own tables and the built-in zh-Hans default.
        transcript_rules = config.transcript_topic_rules
        memory_rules = config.memory_topic_rules
        expression_patterns = None  # Assembler keeps the built-in zh-Hans default
        locale_code = None

    if home is not None:
        root, home_source = Path(home), HOME_FROM_PROCESS
    else:
        root, home_source = resolve_home()
    bound_profile = profile if profile is not None else profile_for_home(root)

    transcript = SessionTranscriptSource(
        root / "state.db",
        max_messages=config.transcript_max_messages,
        window_seconds=float(config.transcript_window_seconds),
        profile=bound_profile,
        clock=clock,
        rules=transcript_rules,
    )
    memory = CrossSessionMemorySource(
        root / "memories",
        window_seconds=float(config.memory_window_seconds),
        profile=bound_profile,
        clock=clock,
        rules=memory_rules,
    )
    sources: Tuple[Any, ...] = (transcript, memory)
    policy = content.ContentPolicy(recent_record_seconds=float(config.recent_record_seconds))

    widest = config.widest_source_window()
    if float(config.recent_record_seconds) < widest:
        # Legal, but it is the assembler — not the sources — that decides what is "recent", so a
        # narrower value silently drops records the sources were configured to offer. Say so once.
        logger.info("proactive-conversation: the assembly window (%.0fs) is narrower than a source window "
                    "(%.0fs); the assembly will drop records the sources offer", 
                    float(config.recent_record_seconds), widest)

    assembler = content.Assembler(sources, policy, toolbox=toolbox, injected_tools=injected_tools,
                                  expression_patterns=expression_patterns)
    return Composition(assembler=assembler, sources=sources, policy=policy, home=root,
                       profile=bound_profile, home_source=home_source, locale=locale_code)


def install(runtime_module: Any, config: CompositionConfig, **kwargs: Any) -> Composition:
    """Build the composition and install it into *runtime_module* (``runtime.set_content``).

    The runtime module is passed in rather than imported, so this can only ever install into the
    plugin's own runtime object (``proactive.runtime`` in this process, or the module hermes loaded
    it as) — never into anything else, and never for a session: only a released wake reaches the
    assembly. Returns the :class:`Composition` for the caller to print/keep.

    The install line names the resolved **home and profile** (plus which evidence produced the home):
    in a multiplexing gateway the only way to tell which data root is live is to read it off the log,
    and a bare profile name cannot distinguish ``~/.hermes`` from
    ``~/.hermes/profiles/<name>``.
    """
    setter = getattr(runtime_module, "set_content", None)
    if not callable(setter):
        raise TypeError("install() needs the plugin runtime module (it must expose set_content)")
    composition = build(config, **kwargs)
    setter(composition.assembler)
    logger.info("proactive-conversation: read-only composition installed (home=%s, profile=%s, home source=%s, "
                "sources=%s)", composition.home, composition.profile, composition.home_source,
                ", ".join(name for name, _ in _named(composition.sources)))
    return composition


def uninstall(runtime_module: Any) -> None:
    """Take the composition out again by clearing the runtime's content seam."""
    setter = getattr(runtime_module, "set_content", None)
    if callable(setter):
        setter(None)


def _named(sources: Sequence[Any]) -> Tuple[Tuple[str, Any], ...]:
    return tuple((str(getattr(source, "name", "") or ""), source) for source in sources or ())


def _as_bindable_count(value: Any) -> Optional[int]:
    """``int(value)`` when *value* is a count the session-store read can really bind; else ``None``.

    ``transcript_max_messages`` travels one more step than the three windows: it becomes the ``?`` in
    ``SELECT … LIMIT ?`` (:mod:`proactive.sources.session_transcript`), and the driver binds integer
    *parameters* as signed 64-bit values. ``2**63`` is an ordinary Python int and a perfectly finite
    ``float`` — so a float screen accepts it — yet the read raises ``OverflowError`` when it binds it.
    A count outside that domain would pass validation, install the assembly, then fail the transcript
    read, leaving the round released from the *other* source: an unusable required count would not
    fail closed.

    The domain therefore comes from the database (``SQLITE_INT_MIN``/``SQLITE_INT_MAX``), not from
    ``float``; ``tests/test_composition.py`` pins the two bounds against a real ``sqlite3`` bind. A
    ``bool`` is refused for the same reason as in :func:`_as_finite_positive` (``True`` is not ``1``
    here), and ``<= 0`` is not a count.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not SQLITE_INT_MIN <= value <= SQLITE_INT_MAX:
        return None
    if value <= 0:
        return None
    return int(value)


#: The integer domain SQLite accepts as a bound parameter — the range ``transcript_max_messages`` is
#: really used in. Values outside it are refused as a field-name problem (:func:`_as_bindable_count`)
#: instead of raising inside the read.
SQLITE_INT_MIN = -(2 ** 63)
SQLITE_INT_MAX = 2 ** 63 - 1


def _as_finite_positive(value: Any) -> Optional[float]:
    """``float(value)`` when *value* is a positive, finite, safely computable number; else ``None``.

    The one screen for every configured window/count. Three inputs an operator
    (or a corrupted file) can produce are refused rather than used:

    * ``bool`` — ``True`` is an ``int`` subclass, but a typo here is not the number 1;
    * ``float('inf')`` / ``float('nan')`` — an infinite window makes *every* record "recent", so a
      fail-open value would widen the read far beyond what was written;
    * an integer too large to convert to a float (``10**400``) — ``float()`` raises
      ``OverflowError``, and a validator that raises is a validator an operator can crash.

    The conversion happens inside ``try`` for exactly that last case: what cannot be computed is a
    *problem name*, never an exception out of :meth:`CompositionConfig.problems`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):  # an int too large for a float is not a window
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _is_positive_number(value: Any) -> bool:
    return _as_finite_positive(value) is not None
