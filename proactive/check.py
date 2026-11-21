"""In-session dry-run of ``session_declarations`` — the ``/proactive-check`` command.

This is the *online* counterpart of ``scripts/validate_session_declarations.py``: an operator can
type ``/proactive-check`` in a CLI or gateway (Feishu) session at any time and see, without touching
anything, whether the currently-effective ``session_declarations`` are legal, which declaration
fails on which field, and what the effective caps / tool switch / prompt mode are.

Design guarantees:

* **one validator** — it calls exactly :func:`proactive.declarations.validate_declarations` →
  :func:`proactive.config.validate`, the *same* pure function the runtime OFF verdict and the
  offline script use. There is no second copy of the rules here.
* **read-only** — it only reads the plugin settings through the host adapter
  (``ctx.get_config`` → ``plugins.entries.<plugin-id>.settings``) and never writes config, state,
  counters or anything durable, and never triggers a delivery.
* **masked identifiers** — a ``session_key`` / ``session_id`` / ``chat_id`` is only ever shown as an
  irreversible short digest (:func:`proactive.content.short_digest`); the plaintext never appears
  in the output.
* **never raises** — the whole handler is wrapped; a fault returns a short readable summary (no
  traceback) and never escapes into the session.

The rendering is deliberately dependency-free (plain strings / ``json``) so it works in a bare
interpreter as well as inside a live gateway session.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from . import config as _config
from . import content as _content
from . import contract as C
from . import declarations as _declarations
from . import host_adapter as _adapter
from . import locales as _locales
from . import settings as _settings
from . import trigger as _trigger

logger = logging.getLogger(__name__)

#: The one flag the command understands: ``/proactive-check --json``.
JSON_FLAG = "--json"

#: The config-nesting shape the offline script understands, so a value written as
#: ``plugins.entries.<plugin>.settings.session_declarations`` is unwrapped here too.
_SETTINGS_PATHS = (
    ("plugins", "entries", C.PLUGIN_NAME, "settings"),
)


def wants_json(raw_args: Any) -> bool:
    """Whether ``--json`` appears as a standalone token in the raw command arguments."""
    if not isinstance(raw_args, str):
        return False
    return JSON_FLAG in raw_args.split()


def mask(value: Any) -> str:
    """An irreversible short digest of an identifier (never the plaintext)."""
    if value is None or value == "":
        return ""
    return _content.short_digest(value)


def _declaration_value(raw: Any) -> Tuple[Any, bool]:
    """``(declarations_value, configured)`` from whatever shape the setting holds.

    Mirrors :func:`scripts.validate_session_declarations._extract_declarations`: a bare list is a
    declaration list, a dict carrying ``session_declarations`` (top-level or under the real
    ``plugins.entries.<plugin>.settings`` nesting) is unwrapped. Anything else is handed to the
    validator unchanged so it can report the shape fault itself. ``configured`` is ``False`` only
    when the key is genuinely unset (``settings.MISSING`` / ``None``).
    """
    if raw is _settings.MISSING or raw is None:
        return _settings.MISSING, False
    if isinstance(raw, list):
        return raw, True
    if isinstance(raw, dict):
        if C.SETTING_SESSION_DECLARATIONS in raw:
            return raw[C.SETTING_SESSION_DECLARATIONS], True
        for prefix in _SETTINGS_PATHS:
            node: Any = raw
            for key in prefix:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if isinstance(node, dict) and C.SETTING_SESSION_DECLARATIONS in node:
                return node[C.SETTING_SESSION_DECLARATIONS], True
    return raw, True


def _problem_dict(problem: "_declarations.DeclarationProblem") -> Dict[str, Any]:
    """One validator problem as a JSON-safe dict, with a field-qualified ``code``.

    A single-field ``off:invalid`` on field ``timezone`` renders as ``off:invalid:timezone`` (the
    validator's exact wording); a cross-field reason such as
    ``off:invalid:window_min_visible>window_max_attempts`` is already complete and is kept verbatim.
    """
    code = problem.reason
    if problem.reason == C.REASON_INVALID and problem.field:
        code = f"{problem.reason}:{problem.field}"
    return {
        "index": problem.index,
        "field": problem.field,
        "reason": problem.reason,
        "code": code,
        "detail": problem.detail,
    }


def _declaration_caps(entry: Any) -> Optional[Dict[str, Any]]:
    """``caps`` for one declaration entry, from the ONE shared parser.

    ``caps`` is ``None`` when the entry's config does not parse (the problem list already explains
    why). Cap values may be ``None`` — that is the parser's "not in effect" (0) representation and is
    rendered as ``0``.
    """
    if not isinstance(entry, dict):
        return None
    config_raw = {
        key: entry[key]
        for key in C.CONFIG_KEYS + C.OPTIONAL_CONFIG_KEYS
        if key in entry
    }
    parsed = _config.parse(config_raw)
    if parsed.config is None:
        return None
    cfg = parsed.config
    return {
        "R_w": cfg.window_max_attempts,
        "V_w": cfg.window_min_visible,
        "S_d": cfg.day_max_silent,
        "R_d": cfg.day_max_attempts,
    }


def _caps_text(caps: Optional[Dict[str, Any]]) -> str:
    if not caps:
        return "n/a"

    def _c(value: Any) -> str:
        return "0" if value is None else str(value)

    return f"{_c(caps['R_w'])}/{_c(caps['V_w'])}/{_c(caps['S_d'])}/{_c(caps['R_d'])}"


def _prompt_mode(setting_key: str) -> str:
    """``custom`` when the operator's template is present and usable, else ``built-in default``."""
    return "custom" if _trigger._configured_template(setting_key) is not None else "built-in default"


def build_report(raw: Any) -> Dict[str, Any]:
    """Build the read-only report dict from the currently-effective declarations value.

    Pure given the value: it reads the tool switch and prompt mode through the host adapter (which is
    the live plugin settings path) but writes nothing. The returned shape is what ``--json`` emits.
    """
    value, configured = _declaration_value(raw)
    problems = _declarations.validate_declarations(value)

    by_index: Dict[int, List[Dict[str, Any]]] = {}
    for problem in problems:
        by_index.setdefault(problem.index, []).append(_problem_dict(problem))
    whole = by_index.get(-1, [])

    entries = value if isinstance(value, list) else []
    declarations: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        entry_problems = by_index.get(index, [])
        caps = _declaration_caps(entry)
        session_key = entry.get("session_key") if isinstance(entry, dict) else None
        session_id = entry.get("session_id") if isinstance(entry, dict) else None
        timezone = entry.get("timezone") if isinstance(entry, dict) else None
        declarations.append({
            "index": index,
            "session_key": mask(session_key),
            "session_id": mask(session_id),
            "timezone": timezone if isinstance(timezone, str) else "",
            "two_layer": True,
            "status": "ok" if not entry_problems else "invalid",
            "caps": caps,
            "problems": entry_problems,
        })

    invalid_count = sum(1 for decl in declarations if decl["status"] == "invalid")
    if whole:
        # The list itself is unusable: nothing parses, and it is one invalid unit, not zero.
        invalid_count = max(invalid_count, 1)

    overview_caps = next((decl["caps"] for decl in declarations if decl["caps"] is not None), None)
    tools_enabled = bool(_trigger.tools_enabled())
    activation = _locales.current_activation()

    return {
        "ok": not problems,
        "configured": configured,
        "count": len(entries),
        "ok_count": sum(1 for decl in declarations if decl["status"] == "ok"),
        "invalid_count": invalid_count,
        "two_layer_active": any(decl["two_layer"] for decl in declarations),
        "caps": overview_caps,
        "proactive_tools_enabled": tools_enabled,
        "proactive_tools_enabled_note": "absent key defaults to false (fail-closed)",
        "locale": activation.code,
        "locale_active": bool(activation.active),
        "locale_source": activation.source,
        "proactive_locale": {
            "code": activation.code,
            "active": bool(activation.active),
            "source": activation.source,
            "filtering": bool(activation.filtering),
            "reason": activation.reason,
        },
        "prompt_soft": _prompt_mode(C.SETTING_PROMPT_SOFT),
        "prompt_forced": _prompt_mode(C.SETTING_PROMPT_FORCED),
        "declarations": declarations,
        "problems": whole,
        "note": "" if configured else (
            f"{C.SETTING_SESSION_DECLARATIONS} is not set — nothing to validate (OK)"),
    }


def render_text(report: Dict[str, Any]) -> str:
    """Render :func:`build_report` as the human-readable ``/proactive-check`` output."""
    lines: List[str] = ["proactive-check — session_declarations dry-run (read-only)"]
    if not report["configured"]:
        lines.append(f"{C.SETTING_SESSION_DECLARATIONS} is not set — nothing to validate (OK)")
    lines.append(
        f"declarations: {report['count']} (ok: {report['ok_count']}, invalid: {report['invalid_count']})")
    lines.append(f"two_layer_active: {str(report['two_layer_active']).lower()}")
    lines.append(
        f"caps (R_w/V_w/S_d/R_d): {_caps_text(report['caps'])} "
        "(overview = first parsed declaration; each declaration below is authoritative)")
    if report["proactive_tools_enabled"]:
        tools_line = "true (key set to true → every tool allowed in a proactive round)"
    else:
        tools_line = "false (absent key defaults to false — fail-closed, no tools)"
    lines.append(f"proactive_tools_enabled: {tools_line}")
    loc = report.get("proactive_locale") or {}
    if loc.get("active"):
        loc_line = f"{loc.get('code')} (active — language rules apply; source: {loc.get('source')})"
    else:
        loc_line = (f"neutral / inactive (no locale filtering; source: {loc.get('source')})")
    lines.append(f"proactive_locale: {loc_line}")
    lines.append(f"prompts: soft={report['prompt_soft']}, forced={report['prompt_forced']}")

    if report["problems"]:
        lines.append("whole-list problems:")
        for problem in report["problems"]:
            note = f" ({problem['detail']})" if problem["detail"] else ""
            lines.append(f"  - {problem['code']}{note}")

    for decl in report["declarations"]:
        label = (f"session_key=sha256:{decl['session_key']}"
                 if decl["session_key"] else "session_key=<unreadable>")
        tz = decl["timezone"] or "?"
        if decl["status"] == "ok":
            lines.append(
                f"#{decl['index']} {label} tz={tz} two_layer={str(decl['two_layer']).lower()} "
                f"caps={_caps_text(decl['caps'])} : OK")
        else:
            lines.append(f"#{decl['index']} {label} tz={tz} : INVALID")
            for problem in decl["problems"]:
                note = f" ({problem['detail']})" if problem["detail"] else ""
                lines.append(f"    - {problem['code']}{note}")

    if report["ok"]:
        lines.append("RESULT: OK — all declarations valid"
                     if report["configured"]
                     else "RESULT: OK — nothing configured, nothing to validate")
    else:
        lines.append(
            "RESULT: INVALID — see the field(s) above; each invalid declaration turns that channel OFF")
    return "\n".join(lines)


def handle_check(raw_args: str = "") -> str:
    """The ``/proactive-check`` slash-command handler: never raises, never writes.

    Reads the currently-effective ``session_declarations`` through the host adapter, reuses the
    runtime validator, and returns either the human-readable report or, when ``--json`` is present, a
    machine-readable JSON document with the same fields (identifiers masked in both). Any fault is
    turned into a short readable summary — never a traceback — so the session keeps running.
    """
    want_json = wants_json(raw_args)
    try:
        report = build_report(_declarations.raw_value())
        if want_json:
            return json.dumps(report, ensure_ascii=False, indent=2)
        return render_text(report)
    except Exception as exc:  # noqa: BLE001 - a self-check must never crash the session
        summary = f"{type(exc).__name__}: {exc}"
        logger.warning("proactive-conversation: /proactive-check failed (%s)", type(exc).__name__)
        if want_json:
            return json.dumps({"ok": False, "error": summary}, ensure_ascii=False, indent=2)
        return f"proactive-check: could not validate session_declarations — {summary}"


# ── optional: the same check as a terminal subcommand (``hermes proactive-check``) ─────────────


def cli_setup(parser: Any) -> None:
    """Add ``--json`` to the ``hermes proactive-check`` argparse subparser and wire its handler."""
    parser.add_argument(JSON_FLAG, dest="json", action="store_true",
                        help="emit machine-readable JSON")
    parser.set_defaults(func=cli_run)


def cli_run(args: Any) -> int:
    """Run the check from the terminal; returns a shell exit status (0 green, 1 problems)."""
    want_json = bool(getattr(args, "json", False))
    output = handle_check(JSON_FLAG if want_json else "")
    print(output)
    if want_json:
        try:
            return 0 if json.loads(output).get("ok") else 1
        except Exception:  # noqa: BLE001 - a non-JSON error summary is still a failure
            return 1
    return 0 if output.rstrip().endswith(("all declarations valid", "nothing to validate")) else 1


__all__ = ["handle_check", "build_report", "render_text", "wants_json", "mask",
           "cli_setup", "cli_run"]
