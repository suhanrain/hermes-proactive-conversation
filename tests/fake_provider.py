"""The scripted provider used by the host-level tests.

A proactive round is a normal agent turn, so there is no plugin-side generation request to route.
This module only prepares the scripted-answer attributes those tests use; every auxiliary call
goes to whatever the fake server already had.
"""

from __future__ import annotations


def install(server) -> None:
    """Prepare *server* for a test run (idempotent per server)."""
    if getattr(server, "_p12_routed", False):
        return
    server.generation_answers = []
    server.generation_requests = []
    server.last_main_text = None
    server._p12_routed = True
