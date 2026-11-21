# Running the tests

[**English**](testing.en.md) | [简体中文](testing.md)

Tests use a **fake model** (LLM responses are stubbed/simulated — **no real model calls, no network, no token spend**), but they do use the real hermes loader, a real `SessionStore` and a real `config.yaml` (all under an isolated temp directory; your profile is never read or written, no messages are sent). You need an interpreter that **has `pytest` installed and can `import hermes`**.

Cases come in two kinds, distinguished by the `integration` marker, with **no external plugins**:

- **Unit tests** (`tests/*.py`) — need no host; they should run anywhere;
- **Integration tests** (`tests/host_e2e/`) — need a **real Hermes host** (`state.db` under `HERMES_HOME`). The marker is applied automatically by `tests/conftest.py` based on the path.

Integration tests are **explicitly opt-in**: having a host installed is not enough.

| Environment | Behaviour |
|---|---|
| `PCHAT_RUN_INTEGRATION=1` unset | integration tests are **all SKIPPED** (host or not); unit tests still run |
| `PCHAT_RUN_INTEGRATION=1` and `state.db` under `HERMES_HOME` | integration tests **really run** |
| `PCHAT_RUN_INTEGRATION=1` but no host | integration tests are SKIPPED with a reason |
| `HERMES_HOST_REQUIRED=1` but no host | **hard failure** (`pytest.exit` with a clear reason) — never a silent green |

Recommended host version: **Hermes Agent v0.23.0**. Host APIs may change; other versions are unverified.

```bash
# local / no host: by default you should only see unit results; host_e2e is explicitly SKIPPED
<python> -m pytest -q -p no:cacheprovider
# unit only
<python> -m pytest -q -m "not integration" -p no:cacheprovider
# integration only when a host really exists (a missing host is a clear error, not a fake pass)
HERMES_HOME=/path/to/live/profile HERMES_HOST_REQUIRED=1 \
    <python> -m pytest -q -m integration -p no:cacheprovider
```
