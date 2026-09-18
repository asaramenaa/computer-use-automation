# Decisions log

One entry per module: what was built, the key decision, the rejected alternative, and why.

## 1. target_app

- Built: Flask stand-in for a legacy member-servicing app. Frameset (nav + main), nested tables, no ids/classes/labels, inputs named only by adjacent cell text. Flow: sign on -> member search -> profile (accounts table with Savings balance) -> open sub-account form -> confirmation. Two roles (teller, auditor) so permission denial is a real behaviour, not only an injected one.
- Key decision: fault injection has two entry points. `?fault=` on a request for unit tests, and `/admin/fault/<kind>?route=&count=` which arms a process-wide fault consumed by the next matching request. Armed faults let a replay hit session timeout / 500 / interstitial without editing the artifact or its inputs.
- Rejected: session-scoped armed faults (cookie-bound). The browser owns the session cookie, so a CLI could not arm a fault for it without sharing cookies. Process-wide is correct for a single-tenant local harness.
- Business errors (not found, validation, denied, expired) render as HTTP 200 pages with human-readable text and legacy error codes (SEC-440, SEC-403, VAL-118). Replay detectors must work from what an operator sees, not status codes, because a UIA/desktop surface has no status codes.
- Interstitial is a real overlay div that blocks clicks until "Continue" is pressed; confirm_dialog is a real `window.confirm` whose cancel path breaks the page. Both exercise recovery paths honestly rather than rendering decorative text.

## 2. schema

- Built: Pydantic v2 `Artifact` (capability contract) and `RunResult` (replay contract), exported to `/schema/*.json` with a test that fails when the export is stale. `Artifact` cross-validates step ids, param references, that declared outputs equal extracted values, and that the last step is covered by a checkpoint.
- Key decision: typed values are `ValueRef`s (`literal` | `param` | `secret`). A `secret` ref carries only an environment variable name and the validator rejects any value on it, so credentials cannot be serialized even by accident. Sensitive inputs must be passed as `secret:<ENV>` and never carry an example.
- Key decision: runtime conditions are three separate lists with different semantics. `outcome_detectors` are terminal business results with a code the caller can branch on. `recoveries` are bounded actions (dismiss, accept_dialog, retry, reauth) with `max_attempts`. Everything else that breaks a postcondition is a hard failure. This is the taxonomy the brief calls load-bearing.
- Rejected: a single `expected_errors` list with a free-text `handling` field. It collapses the business-vs-recoverable distinction and gives the executor nothing to enforce.
- Rejected: storing the LLM transcript or raw a11y snapshots in the artifact for "context". Provenance keeps model, run id, timestamp and the redacted goal only; evidence lives in the run directory.

## 3. surface

- Built: `Surface` protocol (observe, resolve, act, check, wait_for, extract, screenshot, snapshot, dialog policy) and `PlaywrightSurface`. Perception is Chromium's per-frame ARIA snapshot plus an enumerated control list (role, accessible name, nearest preceding visible text as "anchor", masked value). Screenshots are evidence only.
- Key decision: locator resolution order ROLE_NAME -> ANCHOR -> TEXT -> COORDINATES, each strategy must yield exactly one match or it is skipped; the error carries every attempt (strategy, frame, match count). Coordinates are refused when the viewport differs from the recording.
- Key decision: the ANCHOR strategy exists because legacy forms have no `<label>`; the only stable handle is the cell text before the control. Anchor matching runs in-page using the same role rules Playwright's `get_by_role` uses, so both strategies agree on what a control is.
- Rejected: screenshot + coordinates as primary perception (the model would need vision on every step and replay would be viewport-fragile). Rejected: CSS/XPath selectors (nothing to hang them on in table soup, and they do not exist on a desktop surface).
- JS dialogs fail closed: unknown dialogs are dismissed and recorded; only patterns declared in the artifact are accepted. Frames are addressed by name path, not index. Waits are condition polls with a 100 ms interval, never fixed sleeps.

## 4. replay (+ observability)

- Built: `ReplayEngine.run(artifact, params) -> RunResult`. Per step: policy gate -> precondition wait -> resolve + act -> classify (business outcome -> recoverable -> postcondition -> hard failure) -> checkpoints. `detectors.classify` waits until the postcondition *or* any detector/recovery trigger holds, then evaluates in priority order, so a "not found" page is reported immediately instead of after a timeout.
- Key decision: recoveries are bounded and typed. DISMISS clicks a known control and re-acts only if the original action never happened. REAUTH jumps back to the sign-on step and continues linearly. RETRY goes back in history and rewinds to the step after the last verified postcondition, so credentials or form values are re-entered instead of blindly re-clicking on a dead page. Attempts are counted per recovery id across the run; exceeding `max_attempts` is a hard failure that names the recovery.
- Key decision: escalation is an injected `EscalationHandler` protocol (`request(ctx) -> decision`). Replay does not know how a human is reached; the handoff module implements it. On resume the engine scans forward for the furthest step whose postcondition now holds, because a human usually completes more than the one failed step, and logs which steps the human did.
- Rejected: an "assisted LLM fallback" inside replay. It is a stretch goal the brief allows, but it blurs the production-path guarantee the `test_no_llm_in_replay` test enforces. Rejected: sleeping after actions; every wait is a condition poll (100 ms interval) except the retry backoff, which is labelled as such.
- Evidence: JSONL run log with every payload redacted at the write boundary; on hard failure/escalation a full-page screenshot plus a redacted per-frame accessibility snapshot; `result.json` written next to `events.jsonl`.

## 5. policy

- Built: YAML allowlist (origins, glob routes, action types) with an explicit deny list; empty policy denies everything. Risk classification takes the stricter of the artifact's declared class and a control-name heuristic ("Open Account", "Submit", "Transfer"...). Irreversible steps need a human decision (`require_human`) or are blocked outright (`block`); there is no "allow".
- Key decision: secrets by reference only. `ValueRef(kind="secret")` and `secret:<ENV>` params name an environment variable; `SecretStore.resolve` returns a `Secret` with a masked repr and registers the raw value with the `Redactor`, so it is scrubbed from logs, `result.json` and accessibility snapshots even when it appears inside page text (a typed user id is visible in the a11y tree).
- Rejected: a vault/KMS client. The seam is `SecretStore`; env vars are the smallest honest implementation. Rejected: redacting in each caller. One redactor at the logger/evidence boundary is auditable; per-call redaction is forgotten.
- Limits noted for the report: screenshots cannot be redacted, so they are taken only on failure/escalation; balances in outputs are financial data by design (the caller asked for them) and are the caller's responsibility once returned.

## 6. agent (discovery)

- Built: `LLMClient` protocol with `GeminiClient` (google-genai, manual function calling, automatic function calling disabled) and `AnthropicClient` (anthropic SDK, manual tool loop) selected by `LLM_PROVIDER`/`LLM_MODEL`. Both map HTTP 429 to `RateLimited`; `with_backoff` retries with exponential backoff and jitter (honouring Gemini's `retryDelay`), capped by `LLM_MAX_RETRIES`, then raises. SDK-internal retries are disabled so every attempt is visible and bounded. `CUC_MAX_STEPS` caps model turns.
- Built: fixed tool set (navigate, click, type_text, select_option, press_key, extract, handle_dialog, done, escalate). Targets are frame + role + name/anchor, the same vocabulary the artifact uses, so the model can only propose what replay can resolve. Every call passes the allowlist and the risk gate before execution; refusals go back to the model as tool errors, never executed.
- Key decision: the model never sees a credential. It types `{{secret:NAME}}` placeholders that the loop resolves through `SecretStore` at act time; the observation masks password fields and the redactor scrubs the user id from logs and snapshots.
- Key decision: the recorder generalizes by evidence, not by guessing: a typed value becomes a param only if the model tagged it with `param_name` *and* it literally appears in the goal. Postconditions are inferred as the shortest new heading-like line after an action (data rows and redactable lines excluded); the last one becomes the success checkpoint. Outcome detectors and recoveries come from the product profile (`app_profiles/<product>.yaml`), because a happy-path run cannot observe them; the profile is the unit shared across tenants of the same vendor product.
- Rejected: screenshot-based perception (needs a vision model each turn, and coordinates do not survive tenants/viewports). Rejected: letting the model emit CSS/XPath (nothing stable to target in table soup; meaningless on a desktop surface). Rejected: recording raw observations into the artifact.
- Stops: goal met, max steps, wall-clock timeout, dead end (same action on the same state hash three times), escalation. After each action the loop waits for the observed state to settle (changed state must hold for two samples) so postconditions are inferred from the landed page, not a mid-navigation snapshot.

## 7. handoff

- Built: `ControlLease` (holder, state, version, intervention id, resume signal) persisted as `runs/<id>/control.json`; `HandoffMachine` enforcing AUTOMATION -> PAUSED -> HUMAN -> RESUMING -> AUTOMATION (PAUSED -> AUTOMATION only for abandon-before-transfer); `InterventionRequest` JSON with capability, step, reason, url, screenshot, a11y snapshot and the exact resume commands; `HumanActionRecorder` hooking click/change/submit/navigation in every frame of the same page; `CliEscalationHandler` implementing the replay/discovery `EscalationHandler` seam.
- Key decision: the resume signal is a file write by a separate CLI process (`cuc resume --run-id --decision`), polled by the automation process while it pumps the browser event loop. This works from another terminal, survives the operator closing their shell, and leaves an auditable record of who signalled what. The operator console itself is a MOCK: stdout instructions plus a CLI, not a web console.
- Key decision: three decisions, not one. `resumed` (human changed state; automation re-verifies by scanning forward for the furthest step whose postcondition holds), `confirmed` (irreversible step approved; automation performs it), `abandoned` (run ends ESCALATED). A timeout is `abandoned`.
- Human actions are recorded as control identity plus value *length*; typed values are never captured, password fields not even that.
- Rejected: an in-process "press Enter to resume" prompt (blocks the event loop, not addressable from a console). Rejected: opening a fresh browser for the human (loses cookies/session; the brief requires the same live session).

## 8. catalog (stretch)

- Built: `Catalog.tools()` lists artifacts as typed tool definitions (input/output JSON Schema, outcome codes, irreversible flag; newest semver per capability id; invalid files are skipped, never exposed). `Catalog.invoke(name, args, runner)` validates args against the contract before the deterministic runner runs.
- Rejected: an HTTP endpoint. A tool-definition list and an invoke function are the same contract with no server to operate; the brief explicitly does not reward services.

## 9. cli + observability

- Built: `cuc target-app serve | discover | replay | resume | catalog list|invoke | schema export`. Headed browser by default (a human must be able to watch and take over); `--headless` for CI. Run logs are JSONL with the model's reason per decision, resolved strategy per step, classification per step, handoff transitions and human actions.
- Every payload is redacted at the log boundary. `kind` on `RunLog.event` is positional-only so payloads carrying their own `kind` (human events) cannot collide.

## Addendum (found during CLI smoke runs)

- All hard failures (locator miss, failed postcondition, failed checkpoint, exhausted recovery, failed rewind) now raise one internal `_HardFailure` that the step loop routes through evidence capture and the escalation seam. Before this, exhausted recoveries stopped the run without offering the human a turn, which the cross-process `cuc resume` smoke run exposed. Policy denials still stop immediately: a human cannot make a denied action allowed.

## Addendum: Groq provider

- Built: `GroqClient` over Groq's OpenAI-compatible endpoint using the `openai` SDK with `base_url`, chat-completions function calling (`tools` + `tool_choice="auto"`), tool results as `role: tool` messages. Default provider is now `groq` with `openai/gpt-oss-120b`; Gemini and Anthropic remain selectable.
- Key decision: 429 handling reads the `Retry-After` header and waits exactly that (plus 1 s) instead of guessing; without the header it falls back to the shared exponential backoff. SDK-internal retries are disabled so the attempt cap (`LLM_MAX_RETRIES`) is the only retry budget.
- Rejected: hand-rolled HTTP against the endpoint. The `openai` client already types the tool-call shapes and error classes; the adapter stays ~60 lines and injectable for tests.
- Malformed tool-call JSON from the model is surfaced to the loop as `_malformed_arguments` rather than crashing the run; the loop reports it back as a tool error.
- `openai` and `groq` were added to the forbidden import list for the production packages.

## Addendum: what the real discovery runs taught

- Attempt 1 (Groq free tier) died at the last turn with HTTP 413: the tier caps a request at 8,000 tokens and the resent conversation had reached 8,065 because every tool result carried a full observation. Fix: each provider adapter elides earlier observations before sending (only the latest observation is sent in full; earlier ones are stale by definition), frameset containers are skipped in the prompt, and the per-frame cap is 3,500 characters. Per-request size is now roughly constant instead of growing with steps.
- Attempt 2 died on a hung request that the SDK's 10-minute default timeout turned into a connection error, which the loop treated as fatal. Fix: `LLM_REQUEST_TIMEOUT_S` (120 s) on every adapter, and connection errors / 5xx are now `Transient`, retried under the same bounded backoff as 429. 4xx other than 429 stay fatal.
- Both failed runs are kept under `runs/` (gitignored) and referenced from `evidence/README.md`; nothing was edited by hand.
- Attempt 3 met the goal but wandered: the system prompt's example placeholder (`{{secret:APP_USER}}`) did not match the offered names, the model typed it, sign-on failed with SEC-401, and the recorder faithfully kept the failed attempt, giving an artifact whose step 3 "expects" the SEC-401 page. Two fixes: the prompt now points at the listed placeholders only and the unknown-secret error names the available ones; and the recorder prunes the trace by evidence (drop everything up to the last action whose after-state matched a terminal outcome detector; collapse consecutive types into the same control). Both rules are simple enough to explain to a reviewer, and the artifact stays the review surface.
- Instrumentation: every model call is logged with elapsed seconds, attempt count and token usage (`llm_call`), and retry prints carry timestamps, so a slow run can be attributed to the provider or to the browser. `LLM_REASONING_EFFORT=low` for gpt-oss keeps per-turn output small on the free tier.

## Final real runs (2026-09-18)

- Discovery attempt 5 met the goal in 7 actions / 8 model calls (~19.5k input tokens, ~0.6k output; each successful model call took under 1.1 s). The recorded artifact needed no pruning. Unexplained multi-minute gaps between turns in this and earlier runs coincide with the laptop sleeping, not with provider latency, which the per-call timing now makes visible; the README recommends `caffeinate -i` for long runs.
- Replays against that artifact: `SUCCESS` (10047 → 9800.0), `BUSINESS_OUTCOME MEMBER_NOT_FOUND` (99999), `SUCCESS` with `r02` re-authentication after an injected session timeout, and `FAILED` with screenshot + a11y snapshot after `r03` exhausted its two retries against a persistent HTTP 500. All four are in `evidence/`.
- Handoff run (`evidence/handoff-demo/`): replay escalated at s04 after `r03` was exhausted; the operator held the lease for ~3.5 minutes, signed on in the same Chromium window, and signalled `resumed` from a second terminal. The engine found s04's postcondition ("Member Search") holding, marked s04 as done by the human, and completed s05–s07 itself. The human-action log shows the design holding under real use: control identity and value length only, `null` for the password field.
