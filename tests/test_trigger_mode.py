"""``trigger_mode`` resolution: ``on`` | ``off`` only.

Locks two things that are easy to regress:
* YAML 1.1 coerces a bare ``trigger_mode: on`` / ``off`` to a **boolean**, so the reader must honour
  ``True``/``False`` rather than silently falling back to ``off``.
* There is NO ``plugin_injection`` alias any more — a config still carrying it is just an unknown
  value and resolves to ``off`` (fail-closed), never to ``on``.
"""

from __future__ import annotations

import pytest

from proactive import trigger


@pytest.mark.parametrize("raw, expected", [
    ("on", "on"),
    ("off", "off"),
    ("ON", "on"),          # case-insensitive
    (" Off ", "off"),      # whitespace-insensitive
    (True, "on"),          # YAML bare `on`/`yes`/`true` -> bool True
    (False, "off"),        # YAML bare `off`/`no`/`false` -> bool False
    ("plugin_injection", "off"),   # legacy alias REMOVED -> unknown -> fail-closed
    ("bogus", "off"),
    ("", "off"),
    (None, "off"),
    (123, "off"),
])
def test_trigger_mode_resolution(monkeypatch, raw, expected):
    monkeypatch.setattr(trigger._adapter, "config", lambda key, default=None: raw)
    assert trigger.trigger_mode() == expected
