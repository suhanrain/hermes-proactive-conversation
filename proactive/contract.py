"""Frozen names, units and reason codes — the per-session contract in code form.

Units:

* ``min_idle_seconds`` / ``max_idle_seconds`` — the silence window, in **seconds**.
* ``window_max_attempts`` / ``window_min_visible`` / ``day_max_silent`` / ``day_max_attempts`` —
  the two-layer counter model's plain counts (the one counter model; ``day_max_attempts`` optional).
* ``dnd_start`` / ``dnd_end`` — local wall-clock ``HH:MM`` strings, interpreted in the
  session's own IANA ``timezone``.

Per-session configuration has no fallback defaults anywhere in this package: a session is either
configured completely and validly, or it is OFF.

The **plugin-level** settings below are the one exception, and it is a deliberate one: they are
knobs for the plugin as a whole (``plugins.entries.<plugin-id>.settings.*``), each with a documented
default that applies when the key is not set.
"""

from __future__ import annotations

#: Plugin name, and the name its installed directory must carry
#: (``<HERMES_HOME>/plugins/proactive-conversation/``) — the loader keys plugins by directory name.
PLUGIN_NAME = "proactive-conversation"

#: The one plugin-owned per-session metadata key. It holds config + state + dimension
#: fingerprint in a single value, so one read is one consistent view and one write is one
#: consistent update.
META_KEY = "proactive-conversation:session"

#: Blob schema version. A blob written by any other version is ignored (treated as absent),
#: never guessed at and never partially migrated.
BLOB_VERSION = 1

#: The ``origin`` field of the blob, i.e. *who* authorised this session.
#: **Required for authorisation**: a blob with no ``origin`` is NOT authorised (``off:undeclared``).
#: ``declaration`` = the blob was created or last mirrored from ``session_declarations``: the
#: declaration owns this authorisation, so removing that declaration turns the session OFF (its
#: leftover mirror is not an independent authorisation). ``api`` = an explicit programmatic
#: ``enable_session``. A blob whose marker is present but not one of :data:`BLOB_ORIGINS` is invalid
#: → OFF.
ORIGIN_DECLARATION = "declaration"
#: An explicit programmatic enable (``runtime.enable_session``). A distinct, auditable stamp so an
#: API-enabled session is never confused with a bare, un-stamped blob, which is no authorisation at all.
ORIGIN_API = "api"
BLOB_ORIGINS = (ORIGIN_DECLARATION, ORIGIN_API)

#: The six keys every config must carry regardless of the counter model.
REQUIRED_CORE_KEYS = (
    "enabled",
    "timezone",
    "dnd_start",
    "dnd_end",
    "min_idle_seconds",
    "max_idle_seconds",
)

# ── two-layer counter model — the one counter model ───────────────────────────────────────────
# Two layers, "one attempt" = one proactive evaluation (a model ran, visible or silent, it is one):
#
#  * window layer (reset on every user message): ``window_max_attempts`` = R_w, ``window_min_visible``
#    = V_w.  The window's silent cap is **derived** as R_w − V_w and is never configured.  Until V_w
#    visible replies exist in the window a round is forced visible; after that a round may go silent,
#    but silent ≤ R_w − V_w and attempts ≤ R_w; when the window's budget is spent nothing releases
#    until the next user message reopens it.
#  * day layer (reset at the session-local midnight, the token-safety backstop):
#    ``day_max_silent`` = S_d (the main brake, caps silent rounds for the whole day) and the
#    **optional** ``day_max_attempts`` = R_d (a hard cap on the day's total attempts; when set the
#    operator explicitly prefers saving money over sending, so a visible round may be cut too).
#
# ``0`` means "this limit is not in effect" for every knob — except one red line: if ``window_max_attempts``,
# ``day_max_attempts`` and ``day_max_silent`` are ALL 0 / unset there is no limit anywhere and the
# declaration is refused, otherwise the session is back to burning tokens without bound.
SETTING_WINDOW_MAX_ATTEMPTS = "window_max_attempts"
SETTING_WINDOW_MIN_VISIBLE = "window_min_visible"
SETTING_DAY_MAX_SILENT = "day_max_silent"
SETTING_DAY_MAX_ATTEMPTS = "day_max_attempts"

#: The four keys of the two-layer model — the one and only counter schema.  Three are required
#: (see :data:`TWO_LAYER_REQUIRED_KEYS`); ``day_max_attempts`` is the one optional cap.
TWO_LAYER_CONFIG_KEYS = (
    SETTING_WINDOW_MAX_ATTEMPTS,
    SETTING_WINDOW_MIN_VISIBLE,
    SETTING_DAY_MAX_SILENT,
    SETTING_DAY_MAX_ATTEMPTS,
)

#: The three two-layer keys every config MUST carry (``day_max_attempts`` is the one optional cap:
#: absent = 0 = no day-total cap). This is the required counter schema: omitting one is
#: ``off:invalid:<that field>``. There is one model and no fallback path.
TWO_LAYER_REQUIRED_KEYS = (
    SETTING_WINDOW_MAX_ATTEMPTS,
    SETTING_WINDOW_MIN_VISIBLE,
    SETTING_DAY_MAX_SILENT,
)

#: The nine required per-session configuration keys of the one (two-layer) model: the six core keys
#: plus the three required counter keys (``day_max_attempts`` is the one optional cap). Kept whole
#: for the mirror's per-field comparison (:func:`proactive.store.same_config`).
CONFIG_KEYS = REQUIRED_CORE_KEYS + TWO_LAYER_REQUIRED_KEYS

#: The one **optional** per-session key: the day-total attempt cap (``day_max_attempts``).  A
#: declaration passes it through only if it actually wrote it — the parser must never invent a
#: default, because "absent" and "declared as the default" are the same verdict but the mirror has
#: to stay verbatim.  Disjoint from ``CONFIG_KEYS``; the nine required keys are never optional.
OPTIONAL_CONFIG_KEYS = (SETTING_DAY_MAX_ATTEMPTS,)

#: Session authorisation state inside the blob.
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"

# ── plugin-level settings ───────────────────────────────────────────────────────────────────
# Read through the plugin context (``ctx.get_config``) from
# ``plugins.entries.<plugin-id>.settings.<key>``. Each key has a documented default (what an absent
# key means) and an upper bound a configured value must respect; a value that is present but
# unusable is *also* the default — never a silently accepted third value (see proactive/settings.py).

#: Mechanical TTL of an in-flight release ticket: how long a released turn may
#: still be re-checked before delivery, so a crashed or wedged turn can never silence a later,
#: unrelated turn. Default when ``pending_release_ttl_seconds`` is not set.
PENDING_RELEASE_TTL_DEFAULT_SECONDS = 600.0
#: Upper bound for that TTL: a ticket is process-local and consumed by the first re-check, so a
#: day-old ticket cannot belong to a live turn — and a value above this is an obvious unit slip
#: (milliseconds read as seconds), which must not silently become "never expires".
PENDING_RELEASE_TTL_MAX_SECONDS = 24 * 3600.0

#: Mechanical sanity band for an inbound message's own timestamp: a timestamp
#: further from the hook's "now" than this is treated as unusable (clock skew, replay, a wrong
#: value), and the hook falls back to "now" instead of anchoring the window on it. Default when
#: ``event_timestamp_sanity_seconds`` is not set.
EVENT_TIMESTAMP_SANITY_DEFAULT_SECONDS = 7 * 24 * 3600.0
#: Upper bound for that band: it exists to reject implausible timestamps, so a configured band of a
#: month is already generous (a stale anchor drags the window into the past, i.e. releases early),
#: and anything beyond it is a unit slip rather than a product choice.
EVENT_TIMESTAMP_SANITY_MAX_SECONDS = 30 * 24 * 3600.0

#: The plugin-level settings keys, as written in ``plugin.yaml`` ``config_schema``.
SETTING_PENDING_RELEASE_TTL_SECONDS = "pending_release_ttl_seconds"
SETTING_EVENT_TIMESTAMP_SANITY_SECONDS = "event_timestamp_sanity_seconds"

#: The config-file declaration of which sessions may be spoken to. The value
#: is a list of declarations, each carrying ``session_key`` + ``session_id`` and the required
#: ``CONFIG_KEYS`` above (plus the optional ``day_max_attempts``). Absent / empty / no entry for this
#: ``session_key`` = this source says nothing (the per-session metadata key stays the authorisation it
#: already was); unusable = OFF.
SETTING_SESSION_DECLARATIONS = "session_declarations"
#: The composition: the windows and counts the read-only assembly runs with —
#: ``recent_record_seconds``, ``transcript_max_messages``, ``transcript_window_seconds``,
#: ``memory_window_seconds`` are **required** (时间次数不得写死: there is no built-in default), plus the
#: two optional topic-rule tables. The value is read on every hook call; it is parsed into
#: ``composition.CompositionConfig`` and installed into this plugin's own runtime module. Absent,
#: incomplete or illegal means **fail closed**: no assembly is installed and no wake is released
#: (reason ``off:content-unconfigured``), because a released wake without an assembly is a round
#: whose material is the raw heartbeat prompt.
SETTING_COMPOSITION = "composition"

#: Bounds for a transcript window read by the adapter. The default is 10 rows; a window larger
#: than 50 is a whole-session dump, not a window.
SESSION_CONTEXT_ROUNDS_DEFAULT = 10
SESSION_CONTEXT_ROUNDS_MAX = 50

#: Which proactive trigger runs: ``trigger_mode`` is one of ``on`` / ``off`` (default ``off``).
SETTING_TRIGGER_MODE = "trigger_mode"

# ── boolean proactive tool switch + configurable prompts ─────────────────────────────────────
# The switch is boolean:
#
#  * ``true``  — **every** tool is callable in a proactive round.
#  * ``false`` — **no** tool at all is callable in a proactive round:
#    ``pre_tool_call`` refuses every tool with ``denied:proactive-tool``.
#
# It only ever applies to a turn this plugin recognised as **its own** proactive round; an ordinary
# user turn is never affected. The documented default is ``false`` — **fail-closed**: a proactive
# round may call a tool only when the key is *explicitly* set to
# ``true``. An absent key, or any value that is not the boolean ``true``, means "no tools at all", so
# a config that simply forgets the key can never hand a proactive round the full tool set. An
# unusable value (a non-bool: a string, a number, ...) is treated the same as absent (no tools) and
# a warning is logged.
SETTING_TOOLS_ENABLED = "proactive_tools_enabled"
TOOLS_ENABLED_DEFAULT = False

#: The two configurable proactive-prompt templates. Each is a string template
#: rendered with ``str.format`` and exactly the placeholders in :data:`PROMPT_PLACEHOLDERS`; an
#: absent key means the built-in default wording, and an unusable template (not a string, empty,
#: unknown placeholder, or a ``format`` failure such as a stray ``{``) falls back to that built-in
#: default with a warning — the plugin never sends an empty or broken prompt, and never crashes.
SETTING_PROMPT_SOFT = "proactive_prompt_soft"
SETTING_PROMPT_FORCED = "proactive_prompt_forced"
PROMPT_PLACEHOLDERS = ("visible_since_user", "minimum_visible_replies")

#: The locale-activation switch (i18n). It decides whether the language-dependent rules
#: (expression patterns, the pure-acknowledgement table, the length floor) and the built-in default
#: prompt wording are in force, and which :class:`proactive.locales.LocaleProfile` supplies them.
#: ``zh-Hans`` / ``en`` / ``auto`` activate that profile; **unset means neutral** (no language
#: filtering at all — only the global machine markers + structural filters apply, so an unconfigured
#: install never mis-drops an ordinary message); any other value is a WARNING + neutral (fail-safe).
#: ``auto`` reads the host locale and falls back to ``en`` with a WARNING when it cannot.
SETTING_LOCALE = "proactive_locale"
LOCALE_AUTO = "auto"

PLUGIN_SETTING_KEYS = (
    SETTING_PENDING_RELEASE_TTL_SECONDS,
    SETTING_EVENT_TIMESTAMP_SANITY_SECONDS,
    SETTING_SESSION_DECLARATIONS,
    SETTING_COMPOSITION,
    SETTING_TRIGGER_MODE,
    SETTING_TOOLS_ENABLED,
    SETTING_PROMPT_SOFT,
    SETTING_PROMPT_FORCED,
    SETTING_LOCALE,
)

#: Setting-resolution outcome kinds: ``absent`` = the key was not set (the
#: documented default applies, normal); ``invalid`` = the key was set to something the plugin
#: refuses, so the default applied AND a warning was logged.
SETTING_ABSENT = "absent"
SETTING_INVALID = "invalid"


def setting_reason(kind: str, key: str) -> str:
    """``config:<kind>:<key>`` — the machine-readable outcome of one plugin-setting lookup."""
    return f"config:{kind}:{key}"

#: Machine-readable reasons. The release decision returns exactly one of these; tests and the
#: delivery report assert on them instead of on prose.
REASON_OK = "ok"
REASON_NO_BLOB = "off:no-blob"
REASON_DIMS = "off:dims-mismatch"
REASON_DISABLED = "off:disabled"
REASON_INVALID = "off:invalid"
REASON_PAUSED = "paused"
REASON_DND = "dnd"
REASON_DND_RESAMPLE = "dnd:resampled"
REASON_CAP_DAY = "cap:day"
#: The session-local day's silent-round budget (``day_max_silent``) is spent: no further *silent*
#: proactive round is released today (a forced-visible round is unaffected).
REASON_SILENT_CAP = "cap:silent"
#: The two-layer window budget (``window_max_attempts``) is spent: no further attempt is released in
#: this user-message window; the next real user message reopens it.
REASON_CAP_WINDOW = "cap:window"
REASON_WINDOW_OPEN = "window:open"
REASON_WINDOW_DUE = "window:due"
REASON_USER_MESSAGE = "user-message"
#: The wake's session id could not be resolved, so a release could never be counted/re-checked.
REASON_NO_SESSION = "no-session-id"
#: The session's metadata key state could not be read (the store's read raised), so the plugin
#: cannot rule out a permanent close and therefore neither mirrors nor writes. A distinct code from
#: ``off:no-blob`` on purpose: "nothing is configured" and "I cannot see what is there" lead to the
#: same OFF verdict but are not the same fact, and only the second one is a store fault.
REASON_UNVERIFIED = "off:unverified"
#: The session's authorisation came from a declaration and that declaration is gone (the entry
#: is missing, the list is empty, or the setting key is absent), so the leftover mirror is
#: **not** an independent authorisation. An API-enabled blob keeps working — only
#: declaration-owned authorisations need their source.
REASON_UNDECLARED = "off:undeclared"
#: The declaration source itself could not be read (``ctx.get_config`` raised). Fail closed: an
#: unreadable source is not "no declaration", so nothing may fall back to the stored authorisation.
REASON_CONFIG_UNREADABLE = "off:config-unreadable"
#: No usable composition is configured (the ``composition`` setting is absent, incomplete or
#: illegal), so this round has no read-only material to be opened with. A wake is therefore refused
#: instead of handing the raw heartbeat prompt to the model: zero LLM, zero delivery, and **no write**
#: to the session metadata key. A user message is never affected.
REASON_CONTENT_UNCONFIGURED = "off:content-unconfigured"
#: The installed composition is bound to a *different* profile than the round's own scope, so
#: its data root cannot serve this round. A **configuration/assembly** fault, deliberately not
#: ``silent:source-down``: the sources are fine, the assembly is wired to the wrong home (in a
#: multiplexing gateway the process ``HERMES_HOME`` can win, so profile ``default`` gets an
#: assembly for a ``product-manager`` session and every wake looks like a dead source). Refused
#: with a warning naming both profiles; zero LLM, zero delivery, no write. ``silent:*`` is
#: reserved for "the content itself was not good enough".
REASON_HOME_MISMATCH = "off:home-mismatch"
#: The write that would have recorded a round (the release, or the declaration mirror) was refused
#: or raised: nothing durable accounts for it, so it must not be released.
REASON_WRITE_FAILED = "off:write-failed"
REASON_SILENT_SUPERSEDED = "silent:superseded"
REASON_SILENT_PAUSED = "silent:paused"
REASON_SILENT_DND = "silent:dnd"
REASON_SILENT_DIMS = "silent:dims-mismatch"
REASON_SILENT_SESSION = "silent:session-reset"
REASON_SILENT_OFF = "silent:off"

#: Hermes loaded this plugin into its plugin host (``plugins.isolation: host``). Not a runtime
#: reason string — a load-time refusal cause, kept in the contract so the module that decides it
#: (``proactive.isolation``) and every surface that explains it quote one literal.
REASON_DEGRADED_PLUGIN_HOST = "degraded:plugin-host"

#: The gateway's own delivery-decision marker (gateway/response_filters.py). Replacing a turn's
#: text with it makes the round a delivery decision with zero net sends.
SILENCE_MARKER = "[SILENT]"

# ══ content assembly, source whitelist, tool boundary, expression boundary ═══════════════════
#
# This block adds no user-facing configuration: the freshness window is supplied by the composition
# root rather than hard-coded (时间次数不得写死). Sensitive (health) records are excluded
# unconditionally — there is no switch that could let one become citable.

#: The ONLY sources the assembly path may read. Anything else is not reachable through
#: ``content.SourceRegistry`` — the name is not in the table, so there is no interface to call.
SOURCE_SESSION = "session-transcript"    # this session's own transcript
SOURCE_MEMORY = "cross-session-memory"   # the user's cross-session memory

CONTENT_SOURCES = (
    SOURCE_SESSION,
    SOURCE_MEMORY,
)

#: Assembly status.
ASSEMBLY_READY = "ready"
ASSEMBLY_SILENT = "silent"

#: Source read status.
SOURCE_STATUS_OK = "ok"
SOURCE_STATUS_UNAVAILABLE = "unavailable"
SOURCE_STATUS_DENIED = "denied"

#: Why a source contributed nothing. ``unavailable:*`` = the source is treated as unusable; a
#: weaker source is never promoted into a concrete claim on its behalf.
UNAVAILABLE_NOT_COMPOSED = "unavailable:not-composed"
UNAVAILABLE_READ_FAILED = "unavailable:read-failed"

#: Refusal codes produced by the source registry (execution layer) — the evidence that a
#: non-whitelisted source, another session/profile, the network, or a write-capable source is
#: unreachable from the assembly path rather than merely discouraged in a prompt.
DENIED_UNKNOWN_SOURCE = "denied:unknown-source"
DENIED_SCOPE = "denied:scope-mismatch"
DENIED_NETWORK_SOURCE = "denied:network-source"
DENIED_WRITE_SOURCE = "denied:write-source"

#: Refusal codes produced by the tool boundary (execution layer).
DENIED_UNKNOWN_TOOL = "denied:unknown-tool"
DENIED_WRITE_TOOL = "denied:write-tool"
DENIED_SEND_TOOL = "denied:send-tool"
DENIED_PAYMENT_TOOL = "denied:payment-tool"
DENIED_WRITE_ARGS = "denied:write-args"
TOOL_READ_FAILED = "tool:read-failed"

#: The two write tools that exercise the tool boundary (one writes a memory, one sends a message).
TOOL_WRITE_MEMORY = "memory.write"
TOOL_SEND_MESSAGE = "message.send"

#: The block message a released round's refused tool call returns to the model.
#: The reason code sits between these two, so the tool result, the log line and the test assertion
#: all quote one string instead of three paraphrases.
ROUND_GATE_MESSAGE_PREFIX = "proactive-conversation:"
ROUND_GATE_MESSAGE_SUFFIX = "— this proactive round may only use read tools"

#: Why an assembly stays silent. Each of these is a separate verdict, and
#: none of them ever produces a candidate sentence.
SILENT_NO_TOPIC = "silent:no-topic"
SILENT_SOURCE_DOWN = "silent:source-down"
SILENT_STALE = "silent:stale"
SILENT_CONFLICT = "silent:conflict"
SILENT_UNRELATED = "silent:unrelated"
SILENT_HEALTH = "silent:health"
SILENT_EXPRESSION = "silent:expression"
SILENT_POLICY = "silent:policy-incomplete"
SILENT_INTERNAL = "silent:internal-error"

#: The **model itself** answered the released round with the gateway's own exact silence marker,
#: so the round reached nobody and must not spend the user's day. Every ``silent:*``
#: above is a *plugin* verdict taken before the turn; this one is the turn's own delivery decision,
#: taken at the last seam and only for a round this plugin provably released and still owns. It is
#: deliberately not one of the assembly's reasons: an assembly that could not find a topic never
#: opens a round at all, and a marker the *model* writes is not evidence about the sources.
SILENT_MODEL_CHOICE = "silent:model-choice"

#: A round this plugin released and settled was **not** answered by the consume gate, so an
#: ordinary turn ran for it after all. Under the consume shape that turn's text is never the text
#: the user gets: the round publishes nothing rather than the host's own answer.
#: Reaching this code at all means the consumer did not match the wake — the one failure the design
#: cannot rule out from the plugin side — so it is recorded as its own reason instead of being folded
#: into "silent".
SILENT_CONSUME_MISSED = "silent:consume-missed"

#: Rendering bound: how much of a record's own words may be quoted in a candidate.
CANDIDATE_MAX_CHARS = 60

# ── the delivery gate ────────────────────────────────────────────────────────────────────────
# Names used by ``proactive/delivery_guard.py`` only.

#: The only platform a proactive message may be delivered to (the user's Feishu DM).
PLATFORM_FEISHU = "feishu"
#: The only chat type a proactive message may be delivered to: a direct message.
CHAT_TYPE_DM = "dm"

#: Permission state of one delivery attempt. ``active`` is the only state that allows a send;
#: every other state is a refusal that costs zero sends.
PERM_ACTIVE = "active"
#: The grant is bound to one released round and to that round's ticket: no ticket in flight, a
#: ticket past its TTL, or a route whose dimensions do not equal the granted ones.
PERM_EXPIRED = "expired"
#: The user switched the session off (``enabled: false``) while the blob stayed readable.
PERM_REVOKED = "revoked"
PERM_PAUSED = "paused"
#: Permanent close: the plugin's per-session value is JSON ``null``, i.e. deliberately gone
#: rather than never written.
PERM_CLOSED = "closed"
#: No grant was ever stored for this route (no blob, or a blob this plugin cannot read).
PERM_ABSENT = "absent"

REASON_PERM_ACTIVE = "perm:active"
REASON_PERM_EXPIRED = "perm:expired"
REASON_PERM_TTL = "perm:ttl-expired"
REASON_PERM_NO_TICKET = "perm:no-ticket"
REASON_PERM_STALE_ROUTE = "perm:stale-route"
REASON_PERM_REVOKED = "perm:revoked"
REASON_PERM_PAUSED = "perm:paused"
REASON_PERM_CLOSED = "perm:closed"
REASON_PERM_ABSENT = "perm:absent"

REASON_TARGET_NO_SESSION_KEY = "target:no-session-key"
REASON_TARGET_NO_SESSION_ID = "target:no-session-id"
REASON_TARGET_PLATFORM = "target:platform"
REASON_TARGET_NOT_DM = "target:not-dm"
REASON_TARGET_NO_CHAT = "target:no-chat"

#: Delivery states. Exactly one of these describes every attempt, and only
#: ``sent`` may ever be reported or counted as a delivery.
SEND_SENT = "sent"            # a platform receipt identified *this* delivery
SEND_FAILED = "failed"        # the transport asserted non-delivery
SEND_UNCERTAIN = "uncertain"  # no usable receipt: whether the user has the message is unknown
SEND_REFUSED = "refused"      # the gate refused: the transport was never called

REASON_SEND_REJECTED = "send:rejected"
REASON_SEND_NO_RECEIPT = "send:no-receipt"
REASON_SEND_NO_RECEIPT_ID = "send:no-receipt-id"
REASON_SEND_UNRECOGNIZED = "send:unrecognized-receipt"
REASON_SEND_WRONG_TARGET = "send:wrong-target"
REASON_SEND_RAISED = "send:raised"
REASON_SEND_DUPLICATE = "send:duplicate-suppressed"
#: The write-ahead settlement could not be persisted (no ticket left to commit, or the store
#: refused/failed the write). Fail closed: nothing is sent, because a send without a durable record
#: of the spent ticket would reopen exactly the duplicate window that recording the ticket before
#: sending exists to close.
REASON_SETTLE_FAILED = "settle:write-failed"

#: The round was settled but there was no room for its ticket in the consume queue (three open
#: tickets per route). No turn runs and no consumer can answer it, so the wake is
#: dropped: one missed message, counted, and never a second delivery of an older round.
REASON_CONSUME_QUEUE_FULL = "consume:queue-full"
