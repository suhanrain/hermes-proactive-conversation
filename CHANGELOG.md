# Changelog

All notable changes to `proactive-conversation` are documented here. **v1.0.0 is the first public release.**

## v1.0.0 — 2026-11-21

### Added

- **`/proactive-check` command (and `hermes proactive-check` CLI twin).** A read-only,
  in-session dry-run of the effective `session_declarations`: it validates the list with
  the exact same validator the runtime and the offline script use, shows per-declaration
  status, effective caps, the tool switch and prompt mode, and never writes or delivers
  anything. Supports `--json`. Identifiers are only ever shown as short irreversible
  digests — never the plaintext session key, session id or chat id.
- **Configurable prompts.** `proactive_prompt_soft` and `proactive_prompt_forced` let an
  operator replace the built-in wording of the soft (may stay silent) and forced
  (must be visible) rounds. Templates render with `{visible_since_user}` and
  `{minimum_visible_replies}`; an unusable template falls back to the built-in default
  with a warning and never produces an empty or broken prompt.
- **Anti-repetition guidance** in the forced-visible prompt: when a reply is mandatory
  the model is told it already replied and must add something genuinely new instead of
  repeating or paraphrasing.
- **Bilingual documentation.** `README.md` (English) + `README.zh-CN.md` (Chinese), and
  `docs/*.md` / `docs/*.en.md` with a language switcher on every page. The two-layer
  counter model is illustrated in both languages (`docs/assets/two-layer-counters*.svg`).

### Changed

- **Two-layer counting model.** Alongside the per-session caps, the keys
  `window_max_attempts` / `window_min_visible` / `day_max_silent` / `day_max_attempts`
  add a *window* budget that resets on each user message and a *day* token-safety
  backstop. The silent allowance is derived as `window_max_attempts − window_min_visible`,
  so it is never written by hand. Cross-field rules (`V_w ≤ R_w`, `R_d ≥ S_d`) are
  enforced and an all-zero config is refused.
- **Channel-level authorisation with an effective-session clock.** A declaration may omit
  `session_id`, authorising the whole channel by `session_key`; the mirror writes the
  current session id so `/reset` and `/new` self-heal with no hand-editing. An explicit
  `session_id` still pins one exact instance. Timezone/DND and the per-session (and
  per-day) attribution use the session's own effective session id and timezone.
- **Channel declarations hot-reload.** Edits to `session_declarations` and `composition`
  take effect on the next hook with no reload. Counters are treated as facts and the
  config as the gate: a tightened cap applies immediately against the existing counters,
  and an invalid edit keeps the previous good configuration. A **timezone** change is the
  one exception — because the day boundary is bound to the old timezone, it requires a
  gateway restart (or an explicit reset) rather than a live swap.
- **Fail-closed content governance (`composition`).** A released wake carries material
  assembled from the configured read-only sources; when `composition` is absent,
  incomplete, illegal, unreadable or unbindable, no wake is released and a user message
  is never affected.

### Fixed / hardened

- **Boolean tool switch defaults to fail-closed.** `proactive_tools_enabled` is a pure
  boolean whose default is `false`. An absent key, an explicit `null`, or any non-boolean
  value means *no tool is callable* inside a proactive round (a non-boolean also logs a
  warning). Only an explicit `true` opens every tool.
- **A single boolean governs tool availability.** There is no per-tool allow/deny list.

### Security

- **Removed the hidden "metadata-only" authorisation path.** A per-session blob used to be
  accepted as an authorisation merely because it existed (no `origin` stamp), which let a
  session be authorised *without* being named in `session_declarations` — a second,
  invisible switch that the config file did not reflect. Now a blob must carry an explicit
  `origin` stamp (`declaration` for a mirrored declaration, `api` for an explicit
  `enable_session`); a bare/un-stamped blob reads as `off:undeclared`.
  `session_declarations` is the single source of truth for whether a channel receives
  proactive messages.

### Removed

- **Removed the observation-only counters `unanswered` / `day_count`** from the persisted
  state schema. The two-layer model uses `attempt_since_user` / `attempt_count` /
  `visible_since_user` / `visible_count` / `silent_count`. Existing blobs keep loading;
  the dead keys are dropped on the next write.
- **Removed the single-layer counting model and all of its compatibility code.** The
  `max_unanswered` / `max_per_day` / `max_silent_per_day` / `minimum_visible_replies`
  per-session keys, the dual-model dispatch (`SessionConfig.two_layer`,
  `config.two_layer_active`, the old `policy` cap branch), the old helpers
  (`silent_cap_reached`, `minimum_visible_replies_needed`), the old-only reason code
  (`cap:unanswered`), and the "legacy keys ignored" warning path are all gone. The
  two-layer model is the **only** counter schema; a config that omits a required
  two-layer key is `off:invalid` on that field. The forced-visible prompt reads
  `window_min_visible` directly, and force-every-round is expressed as
  `window_min_visible == window_max_attempts`.
- **Removed configuration aliases and developer drafts from the public tree.**
  `plugin_injection` is no longer accepted as a value of `trigger_mode` (only `on`/`off`,
  plus YAML boolean forms); one-off probe/repro scripts are excluded from the release.

### Unchanged promises

- A real user message is never blocked or silenced by this plugin.
- Every refusal is logged as a reason code plus a short digest — never a plaintext
  session key, session id, chat id, message body, or exception body.

## Release verification — v1.0.0

- **Privacy / leak gate: PASS (0 hits).** Re-run `scripts/no_leak_gate.sh` before
  publishing; it scans the public tree and its commit history for chat/open ids, real
  session keys, e-mail addresses, bearer tokens, `sk-` API keys and credential
  assignments.
- **No real identifiers ship.** All example chat ids / session ids / session keys in the
  code, tests, scripts and the READMEs are all-zero or obviously fake placeholders
  (e.g. `oc_0000000000000000000000000000000`, `20260101_000000_deadbeef`).
- **Public branch is a clean orphan.** `main` is started from a fresh single commit (no
  development history), so id-bearing commits from inside the project never reach the
  public branch.
- **Scope.** The public tree contains code (`__init__.py`, `proactive/`), `plugin.yaml`,
  `README.md`, `README.zh-CN.md`, `LICENSE`, `CHANGELOG.md`, `conftest.py`, `docs/`
  (bilingual pages plus `docs/assets/`), `tests/` and `scripts/`.
