"""Per-session configuration: parse and validate.

Rule: a session is OFF unless every required key is present **and** valid. There is no
default-value path: a config missing ``window_max_attempts`` is OFF even if everything else looks
complete. Parsing therefore returns either a fully populated :class:`SessionConfig` or a reason
string naming the field that made it unusable.

The one counter model is the **two-layer** model: ``window_max_attempts``
(R_w), ``window_min_visible`` (V_w), ``day_max_silent`` (S_d) and the optional ``day_max_attempts``
(R_d). These plus the six core keys are the whole schema — a missing required key is
``off:invalid`` on that field. There is exactly one counter model: no fallback and no dual-model dispatch.

The single source of truth for "is this config usable" is :func:`validate`, a **pure** function
that returns *every* problem (field + reason) it can find. :func:`parse` is a thin wrapper that
turns the first problem into a :class:`ParseResult`, and the offline dry-run script
(``scripts/validate_session_declarations.py``) calls the same :func:`validate` — so an offline
"green" and a runtime OFF can never disagree (:func:`validate` is common to both paths).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from . import contract as C

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionConfig:
    """A complete, validated per-session configuration (all values from user configuration).

    Only the two-layer counter model exists: the four ``window_*`` / ``day_*`` fields below drive
    ``policy``. ``window_min_visible`` is also what the prompt path reads to decide a forced-visible
    round (``window_min_visible`` is the one place this is configured).
    """

    enabled: bool
    timezone: str
    tz: ZoneInfo
    dnd_start_minute: int
    dnd_end_minute: int
    min_idle_seconds: int
    max_idle_seconds: int
    #: ``window_max_attempts`` (R_w): max attempts per user-message window; ``None``/``0`` = no cap.
    window_max_attempts: Optional[int] = None
    #: ``window_min_visible`` (V_w): forced-visible minimum per window; silent cap = R_w − V_w.
    window_min_visible: int = 0
    #: ``day_max_silent`` (S_d): max silent rounds per session-local day; ``None``/``0`` = no cap.
    day_max_silent: Optional[int] = None
    #: ``day_max_attempts`` (R_d): optional hard cap on the day's total attempts; ``None``/``0`` = off.
    day_max_attempts: Optional[int] = None


@dataclass(frozen=True)
class ParseResult:
    """``config`` is set only when every field parsed; ``reason`` is ``""`` exactly then."""

    config: Optional[SessionConfig]
    reason: str
    field: str = ""

    @property
    def ok(self) -> bool:
        return self.config is not None and not self.reason


@dataclass(frozen=True)
class Problem:
    """One reason a config is unusable: the field that caused it, a machine reason and a detail.

    ``reason`` is a ``contract.REASON_*`` code (``off:invalid`` for a single-field fault, or a richer
    ``off:invalid:<field><op><field>`` for a cross-field one); ``detail`` is a short human note that
    **never** carries a session key or a chat id. It may quote values the operator
    already wrote into their own config file (e.g. ``window_min_visible=3``) — that is their own text.
    """

    field: str
    reason: str = C.REASON_INVALID
    detail: str = ""

    @property
    def label(self) -> str:
        if self.field and self.detail:
            return f"{self.field} ({self.detail})"
        return self.field or self.reason


def _invalid(field: str, reason: str = C.REASON_INVALID) -> ParseResult:
    return ParseResult(None, reason, field)


def _as_int(value: Any) -> Optional[int]:
    """Strict integer: ``bool`` is not an int here, floats are not silently truncated."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _parse_hhmm(value: Any) -> Optional[int]:
    """``"HH:MM"`` (or ``"H:MM"``) → minutes since local midnight; ``None`` when unparseable."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) not in (4, 5) or text.count(":") != 1:
        return None
    hh, mm = text.split(":")
    if not (hh.isdigit() and mm.isdigit() and len(mm) == 2):
        return None
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _opt_int(raw: Any, key: str, problems: List[Problem]) -> Tuple[bool, Optional[int]]:
    """Validate one optional integer key; returns ``(ok, value_or_None)`` and appends a problem.

    A key that is absent is fine (``(True, None)``). A key that is present but not a non-negative
    strict integer is a problem and yields ``(False, None)`` so cross-checks cannot cascade on it.
    """
    if key not in raw:
        return True, None
    value = _as_int(raw[key])
    if value is None or value < 0:
        problems.append(Problem(key, C.REASON_INVALID, "must be a non-negative integer"))
        return False, None
    return True, value


def validate(raw: Any) -> List[Problem]:
    """Every reason *raw* is unusable as a per-session config, in a fixed, deterministic order.

    Pure and host-free. Empty list ⟺ the config is complete and valid (a fully-specified but
    ``enabled: false`` config is *valid*, just switched off). This is the one function the parser
    and the offline dry-run script share, so a dry-run failure set and the runtime OFF set agree.
    """
    problems: List[Problem] = []
    if raw is None:
        return [Problem("", C.REASON_NO_BLOB, "no config blob")]
    if not isinstance(raw, dict):
        return [Problem("config", C.REASON_INVALID, "not a mapping")]

    # The schema is one: the six core keys plus the three required two-layer keys
    # (window_max_attempts / window_min_visible / day_max_silent; day_max_attempts is the one
    # optional cap). Keep validating the keys that are present so the operator sees every problem
    # at once instead of only the first missing key.
    for key in C.CONFIG_KEYS:
        if key not in raw:
            problems.append(Problem(key, C.REASON_INVALID, "required key is missing"))

    # ── enabled ──────────────────────────────────────────────────────────────────────────────
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        problems.append(Problem("enabled", C.REASON_INVALID, "must be true or false"))

    # ── timezone ─────────────────────────────────────────────────────────────────────────────
    if "timezone" in raw:
        tz_name = raw["timezone"]
        ok = isinstance(tz_name, str) and bool(tz_name.strip())
        if ok:
            try:
                ZoneInfo(tz_name.strip())
            except Exception:  # ZoneInfoNotFoundError / ValueError / OSError — all "unusable"
                ok = False
        if not ok:
            problems.append(Problem("timezone", C.REASON_INVALID, "not an IANA timezone name"))

    # ── dnd window ───────────────────────────────────────────────────────────────────────────
    start = end = None
    start_ok = end_ok = True
    if "dnd_start" in raw:
        start = _parse_hhmm(raw["dnd_start"])
        start_ok = start is not None
        if not start_ok:
            problems.append(Problem("dnd_start", C.REASON_INVALID, "not an HH:MM time"))
    if "dnd_end" in raw:
        end = _parse_hhmm(raw["dnd_end"])
        end_ok = end is not None
        if not end_ok:
            problems.append(Problem("dnd_end", C.REASON_INVALID, "not an HH:MM time"))
    if start_ok and end_ok and start is not None and end is not None and start == end:
        # A zero-length or 24h DND window is not interpretable: equal bounds are invalid.
        problems.append(Problem("dnd_end", C.REASON_INVALID, "dnd_start and dnd_end must differ"))

    # ── idle window ──────────────────────────────────────────────────────────────────────────
    min_idle = max_idle = None
    min_idle_ok = max_idle_ok = True
    if "min_idle_seconds" in raw:
        min_idle = _as_int(raw["min_idle_seconds"])
        min_idle_ok = min_idle is not None and min_idle > 0
        if not min_idle_ok:
            problems.append(Problem("min_idle_seconds", C.REASON_INVALID,
                                    "must be a positive integer"))
    if "max_idle_seconds" in raw:
        max_idle = _as_int(raw["max_idle_seconds"])
        if max_idle is None or (min_idle_ok and min_idle is not None and max_idle < min_idle):
            problems.append(Problem("max_idle_seconds", C.REASON_INVALID,
                                    "must be an integer >= min_idle_seconds"))
            max_idle_ok = False

    # ── two-layer keys ───────────────────────────────────────────────────────────────────────
    rw_ok, rw = _opt_int(raw, C.SETTING_WINDOW_MAX_ATTEMPTS, problems)
    vw_ok, vw = _opt_int(raw, C.SETTING_WINDOW_MIN_VISIBLE, problems)
    sd_ok, sd = _opt_int(raw, C.SETTING_DAY_MAX_SILENT, problems)
    rd_ok, rd = _opt_int(raw, C.SETTING_DAY_MAX_ATTEMPTS, problems)

    if rw_ok and vw_ok and sd_ok and rd_ok:
        # The four two-layer keys are the whole schema. A missing required key was already reported
        # above as ``off:invalid:<field>``; here we only cross-check the values that exist (and the
        # all-zero red line).
        # V_w ≤ R_w (when a window cap exists).
        if rw is not None and rw > 0 and vw is not None and vw > rw:
            problems.append(Problem(
                C.SETTING_WINDOW_MIN_VISIBLE,
                f"{C.REASON_INVALID}:{C.SETTING_WINDOW_MIN_VISIBLE}>{C.SETTING_WINDOW_MAX_ATTEMPTS}",
                f"{C.SETTING_WINDOW_MIN_VISIBLE}={vw} > "
                f"{C.SETTING_WINDOW_MAX_ATTEMPTS}={rw}",
            ))
        # R_d ≥ S_d (when a day total cap exists).
        if rd is not None and rd > 0 and (sd or 0) > rd:
            problems.append(Problem(
                C.SETTING_DAY_MAX_ATTEMPTS,
                f"{C.REASON_INVALID}:{C.SETTING_DAY_MAX_SILENT}>{C.SETTING_DAY_MAX_ATTEMPTS}",
                f"{C.SETTING_DAY_MAX_SILENT}={sd} > "
                f"{C.SETTING_DAY_MAX_ATTEMPTS}={rd}",
            ))
        # All-zero red line: no limit anywhere → unbounded token burn → refuse the declaration.
        if (rw in (None, 0)) and (rd in (None, 0)) and (sd in (None, 0)):
            problems.append(Problem(
                C.SETTING_WINDOW_MAX_ATTEMPTS,
                f"{C.REASON_INVALID}:no-effective-limit",
                "window_max_attempts, day_max_silent and day_max_attempts are all 0/unset",
            ))

    return problems


def _build(raw: dict) -> SessionConfig:
    """Build the config from a raw mapping already proven valid by :func:`validate`."""
    tz_name = raw["timezone"].strip()
    tz = ZoneInfo(tz_name)
    min_idle = int(raw["min_idle_seconds"])
    max_idle = int(raw["max_idle_seconds"])
    # The two-layer keys are the schema; ``day_max_attempts`` is the one optional cap.
    rw = _as_int(raw[C.SETTING_WINDOW_MAX_ATTEMPTS])
    vw = _as_int(raw[C.SETTING_WINDOW_MIN_VISIBLE])
    sd = _as_int(raw[C.SETTING_DAY_MAX_SILENT])
    rd_raw = raw.get(C.SETTING_DAY_MAX_ATTEMPTS, None)
    rd = _as_int(rd_raw) if rd_raw is not None else None

    return SessionConfig(
        enabled=bool(raw["enabled"]),
        timezone=tz_name,
        tz=tz,
        dnd_start_minute=_parse_hhmm(raw["dnd_start"]),
        dnd_end_minute=_parse_hhmm(raw["dnd_end"]),
        min_idle_seconds=min_idle,
        max_idle_seconds=max_idle,
        window_max_attempts=rw,
        window_min_visible=vw or 0,
        day_max_silent=sd,
        day_max_attempts=rd,
    )


def parse(raw: Any) -> ParseResult:
    """Validate a raw per-session config mapping; never falls back to a default.

    Runs :func:`validate` (the shared, pure source of truth) and turns the first problem into the
    result. ``enabled: false`` on an otherwise valid config is a *disabled* result (a valid but
    switched-off state), not an invalid one.
    """
    problems = validate(raw)
    if problems:
        first = problems[0]
        return ParseResult(None, first.reason, first.field)
    config = _build(raw)
    if not config.enabled:
        return ParseResult(config, C.REASON_DISABLED)
    return ParseResult(config, "")


# ══ hot reload of a channel declaration's config ═══════════════════════════════════════════════
#
# Changing a channel's per-session declaration in config.yaml must take effect on the **next tick**,
# without a gateway restart. The invariant that makes that safe:
#
#   **counters are the facts; the config is only the gate.** A hot reload swaps the gate and never
#   touches the counters. The new thresholds are measured against the *existing* counters at the
#   next ``policy`` decision: a change that tightens a cap below what has already happened caps
#   immediately; it never refunds or resets a day count (config change ≠ "clear the daily quota").
#
# Two changes are refused outright and the previous good config is kept (with a deduped warning):
#   * an **invalid** new config — never fall open to stale numbers *or* to a half-parsed one;
#   * a **timezone** change — ``state.day`` belongs to the timezone it was computed in, so switching it live
#     would either double-count a day or be mistaken for a midnight rollover, which is a way around
#     the daily cap. Folding a timezone edit is a restart (or an explicit reset), not a hot reload.

#: Warnings already emitted (keyed by the new declaration's content hash) so a bad edit that is
#: re-read on every tick logs once instead of flooding the log.
_RELOAD_WARNED: set = set()
#: ``(old_hash, new_hash)`` transitions already reported at INFO — an adopted change logs once.
_RELOAD_INFO_LOGGED: set = set()


def content_hash(raw: Any) -> str:
    """A short, canonical content hash of a raw config mapping (change detection + id-free logs)."""
    try:
        blob = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # a non-JSON value still yields a stable-enough token
        blob = repr(raw)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:12]


@dataclass(frozen=True)
class ReloadResult:
    """Outcome of comparing a declaration's current config with the one already in force.

    ``kind`` is one of:

    * ``unchanged`` — same content (fast path: not even parsed);
    * ``adopted``   — ``config``/``config_raw`` are the new values to use and persist;
    * ``invalid``   — the new config does not parse: keep ``config``/``config_raw`` (the old good one);
    * ``tz-blocked`` — the new config is valid but changes the timezone: keep the old one.
    """

    kind: str
    old_config: SessionConfig
    config: SessionConfig
    config_raw: Dict[str, Any]
    old_hash: str
    new_hash: str
    detail: str = ""


def reconcile_config(old_config: SessionConfig, old_raw: Any, new_raw: Any) -> ReloadResult:
    """Decide whether a freshly-read declaration config replaces the one in force.

    Pure except for :func:`content_hash`; it never touches any counter. The caller keeps every
    counter and simply uses ``result.config`` / ``result.config_raw`` as the gate from now on.
    """
    old_hash = content_hash(old_raw)
    new_hash = content_hash(new_raw)
    old_dict = dict(old_raw) if isinstance(old_raw, dict) else {}
    if old_hash == new_hash:
        return ReloadResult("unchanged", old_config, old_config, old_dict, old_hash, new_hash)
    parsed = parse(new_raw)
    if not parsed.ok:
        return ReloadResult("invalid", old_config, old_config, old_dict, old_hash, new_hash,
                            parsed.reason or C.REASON_INVALID)
    if parsed.config.timezone != old_config.timezone:
        return ReloadResult("tz-blocked", old_config, old_config, old_dict, old_hash, new_hash)
    new_dict = dict(new_raw) if isinstance(new_raw, dict) else {}
    return ReloadResult("adopted", old_config, parsed.config, new_dict, old_hash, new_hash)


def _caps_text(cfg: SessionConfig) -> str:
    """``R_w/V_w/S_d/R_d`` for the log line; ``0`` for an unset/None cap."""
    def _c(value: Optional[int]) -> str:
        return "0" if value is None else str(value)
    return f"{_c(cfg.window_max_attempts)}/{_c(cfg.window_min_visible)}/" \
           f"{_c(cfg.day_max_silent)}/{_c(cfg.day_max_attempts)}"


def log_reload(result: ReloadResult, state: Any = None) -> None:
    """Emit the deduped, identifier-free log line for a :func:`reconcile_config` outcome.

    Never names the session/chat: only content hashes, cap values and counters. ``adopted``
    is INFO (one line per transition); ``invalid``/``tz-blocked`` are WARNINGs (one per bad hash).
    """
    if result.kind == "invalid":
        if result.new_hash not in _RELOAD_WARNED:
            _RELOAD_WARNED.add(result.new_hash)
            logger.warning(
                "proactive-conversation: session_declarations hot-reload rejected (hash %s): invalid "
                "config (%s); keeping the previous good configuration",
                result.new_hash, result.detail or C.REASON_INVALID)
    elif result.kind == "tz-blocked":
        if result.new_hash not in _RELOAD_WARNED:
            _RELOAD_WARNED.add(result.new_hash)
            logger.warning(
                "proactive-conversation: session_declarations hot-reload rejected (hash %s): the timezone "
                "cannot be changed live (state.day is bound to %s); a gateway restart is required "
                "— keeping the old timezone", result.new_hash, result.old_config.timezone)
    elif result.kind == "adopted":
        pair = (result.old_hash, result.new_hash)
        if pair not in _RELOAD_INFO_LOGGED:
            _RELOAD_INFO_LOGGED.add(pair)
            attempt = getattr(state, "attempt_count", None)
            silent = getattr(state, "silent_count", None)
            counters = ""
            if attempt is not None and silent is not None:
                counters = f", counters kept attempt={attempt} silent={silent}"
            logger.info(
                "proactive-conversation: session_declarations hot-reloaded (hash %s→%s): caps "
                "R_w/V_w/S_d/R_d %s → %s%s",
                result.old_hash, result.new_hash,
                _caps_text(result.old_config), _caps_text(result.config), counters)


# ── clock helpers (always in the session's own timezone) ─────────────────────────────────────


def in_dnd(now: float, cfg: SessionConfig) -> bool:
    """Whether ``now`` falls inside the configured DND interval.

    Half-open ``[start, end)``, cross-midnight safe: the start minute is inside, the end minute is
    not — exactly the moment :func:`dnd_end_ts` returns, so "DND ended" and "sending allowed" agree.
    """
    local = datetime.fromtimestamp(now, cfg.tz)
    minute = local.hour * 60 + local.minute
    start, end = cfg.dnd_start_minute, cfg.dnd_end_minute
    if start < end:
        return start <= minute < end
    # Cross-midnight window, e.g. 23:00–09:00.
    return minute >= start or minute < end


def dnd_end_ts(now: float, cfg: SessionConfig) -> float:
    """Epoch seconds at which the DND interval containing ``now`` ends (``now`` inside DND)."""
    local = datetime.fromtimestamp(now, cfg.tz)
    minute = local.hour * 60 + local.minute
    start, end = cfg.dnd_start_minute, cfg.dnd_end_minute
    end_h, end_m = divmod(end, 60)
    same_day_end = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start < end:
        # Non-wrapping window: today's end.
        candidate = same_day_end
    elif minute >= start:
        # Wrapping window, we are in its evening leg: the end is tomorrow morning.
        candidate = same_day_end + timedelta(days=1)
    else:
        # Wrapping window, we are in its morning leg: the end is later today.
        candidate = same_day_end
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def local_day(now: float, cfg: SessionConfig) -> str:
    """The session-local natural day (``YYYY-MM-DD``) used by the daily cap."""
    return datetime.fromtimestamp(now, cfg.tz).strftime("%Y-%m-%d")
