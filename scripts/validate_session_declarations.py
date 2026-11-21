#!/usr/bin/env python3
"""Offline dry-run: validate ``session_declarations`` before you go live.

Reads a config file (JSON, or YAML when PyYAML is installed), pulls out the
``session_declarations`` list from any of the shapes it is normally written in, and runs it through
the **exact same** :func:`proactive.declarations.validate_declarations` →
:func:`proactive.config.validate` the running plugin uses. So a green run here means the sessions
validate the same way they will at runtime; a red run names every offending declaration and field,
and the script exits non-zero. There is no second, drifting copy of the rules.

Why this exists: a mistyped cap (``window_min_visible`` > ``window_max_attempts``,
all limits ``0``, ``day_max_silent`` > ``day_max_attempts``, a bad type) silently turns that one
channel OFF at runtime. This script is the operator-visible check that catches it before shipping.

Usage::

    python scripts/validate_session_declarations.py path/to/config.yaml
    python scripts/validate_session_declarations.py -            # read JSON from stdin
    python scripts/validate_session_declarations.py --declarations '[{...}, ...]'

Exit codes: ``0`` all green; ``1`` one or more invalid declarations; ``2`` could not read/parse the
input (a usage error, not a validation verdict). Zero host coupling: it only needs the plugin
package on ``sys.path`` (it adds the repository root itself) and, for YAML, PyYAML.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, List, Optional, Tuple

# Make ``import proactive`` work no matter where the script is invoked from (this file lives in
# ``scripts/``; the package is one directory up).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from proactive import contract as C  # noqa: E402
from proactive import declarations as _declarations  # noqa: E402

_SETTINGS_PATH = (("plugins", "entries", C.PLUGIN_NAME, "settings"),)


def _load_file(path: str) -> Any:
    """Parse a JSON/YAML file (``-`` = stdin). Raises on an unparseable input."""
    if path == "-":
        text = sys.stdin.read()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"validate-declarations: stdin is not JSON ({exc})")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    lowered = path.lower()
    if lowered.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise SystemExit(
                "validate-declarations: PyYAML is required to read a .yaml file "
                "(install it, or pass a .json file / stdin)")
        return yaml.safe_load(text)
    return json.loads(text)


def _extract_declarations(data: Any) -> Tuple[Any, Optional[str]]:
    """Return ``(declarations_value, where_found)`` from whatever shape the config was written in.

    Understands: a bare declaration list; a dict with a top-level ``session_declarations``; and the
    real ``plugins.entries.<plugin>.settings.session_declarations`` nesting. Returns
    ``(None, None)`` when the key is simply not present (nothing to validate = green).
    """
    if data is None:
        return None, None
    if isinstance(data, list):
        return data, "top-level list"
    if isinstance(data, dict):
        if C.SETTING_SESSION_DECLARATIONS in data:
            return data[C.SETTING_SESSION_DECLARATIONS], C.SETTING_SESSION_DECLARATIONS
        for prefix in _SETTINGS_PATH:
            node: Any = data
            for key in prefix:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if isinstance(node, dict) and C.SETTING_SESSION_DECLARATIONS in node:
                dotted = ".".join(prefix) + "." + C.SETTING_SESSION_DECLARATIONS
                return node[C.SETTING_SESSION_DECLARATIONS], dotted
    return None, None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run validate proactive-conversation session_declarations (offline, no host).")
    parser.add_argument("config", nargs="?",
                        help="path to a config file (JSON, or YAML with PyYAML); '-' = stdin JSON")
    parser.add_argument("--declarations", default=None,
                        help="a JSON array of declarations, passed inline (instead of a file)")
    args = parser.parse_args(argv)

    if args.declarations is not None:
        try:
            data: Any = json.loads(args.declarations)
        except json.JSONDecodeError as exc:
            print(f"validate-declarations: --declarations is not JSON ({exc})", file=sys.stderr)
            return 2
        where = "--declarations"
    elif args.config is not None:
        data = _load_file(args.config)
        where = args.config
    else:
        parser.print_help(sys.stderr)
        print("\nvalidate-declarations: no input given", file=sys.stderr)
        return 2

    declarations, found_at = _extract_declarations(data)
    if found_at is None:
        print(f"validate-declarations: no {C.SETTING_SESSION_DECLARATIONS!r} key found in {where} "
              f"— nothing to validate (OK)")
        return 0

    problems = _declarations.validate_declarations(declarations)
    count = len(declarations) if isinstance(declarations, list) else 0
    if not problems:
        print(f"validate-declarations: OK — {count} declaration(s) in {found_at} are all valid")
        return 0

    print(f"validate-declarations: {len(problems)} problem(s) in {found_at} "
          f"(each invalid declaration makes that channel OFF):")
    for problem in problems:
        print(f"  - {problem.line()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
