# Design report

## 1. Architecture

One Python process, no services, no queues. Five packages with one-way dependencies:

`schema` (contracts) ← `surface` (perceive/act) ← `replay` (deterministic executor) ← `handoff` (human control transfer) and, separately, `agent` (LLM discovery + recorder). `policy` and `observability` are used by everything. `agent` imports `replay` only for the `EscalationHandler` seam; `replay` never imports `agent` or any model SDK, and a test enforces that by parsing imports.

The flow is: `cuc discover` runs an observe → decide → act loop with a fixed tool set, records a trace, and the recorder turns the trace into an `Artifact`. `cuc replay` loads an artifact plus typed params and executes it with no model, returning a `RunResult`. `cuc catalog` exposes artifacts as typed tools and invokes replay by name.

Key decisions and trade-offs:

- **Perception is the accessibility tree, not pixels.** Every frame's ARIA snapshot plus an enumerated control list (role, accessible name, nearest preceding text, masked value). This is what a screen reader sees, it exists on desktop surfaces too, and it needs no vision model on replay. The cost is that a control invisible to the tree (canvas, image map) is invisible to us; that is a known limit, not a surprise.
- **Playwright + Chromium** because it gives the accessibility tree across framesets, actionability checks (an overlay intercepting a click is a real failure), dialog interception, and one headed session a human can take over.
- **Groq (`openai/gpt-oss-120b`) by default, Gemini and Anthropic optional**, behind a small `LLMClient` protocol. The loop sees tool calls in and tool results out; conversation state lives in the provider adapter. Rate limits are retried with exponential backoff (honouring `Retry-After` / `retryDelay` when the provider sends one) under a hard attempt cap; steps are capped. Discovery is the only place a model runs.
- **Local hostile target app** (frameset, nested tables, no ids, no labels, business errors as HTTP 200 pages) rather than a public site. It let me inject the runtime conditions the brief cares about instead of hoping for them, and it keeps real credentials and PII out of the project.
- **Headed browser by default.** The handoff requirement is that a human operates the same session; a headless run cannot be handed over.

## 2. Artifact schema

The artifact is a contract an agent can call, not a step list. Top level: `schema_version`, `capability_id`, semver `version`, `description`, `app` (vendor, product, version, tenant, entry url), typed `inputs`, typed `outputs`, `steps`, `checkpoints`, `outcome_detectors`, `recoveries`, `provenance`. Exported JSON Schema lives in `schema/`; a test fails when it is stale.

- **Steps** carry an action, an ordered `locators` list with a written `rationale` per locator, a `ValueRef`, a `risk_class` (read / reversible / irreversible), optional `precondition` and `postcondition`, a per-step timeout, and for extract steps an `ExtractSpec`.
- **Locators** are one of `role_name`, `anchor` (nearest preceding visible text; the only stable handle in label-less legacy forms), `text`, `coordinates` (last resort, carries the recorded viewport and is refused if it differs). Replay tries them in order and stops at the first strategy that yields exactly one match; more than one match is a miss, never a guess.
- **Values are references.** `literal` (may template `{param}`), `param` (from inputs), `secret` (an environment variable name, and the validator rejects any value on it). Sensitive inputs must be passed as `secret:<ENV>` and cannot carry an example. Credentials therefore cannot be serialized even by mistake.
- **Runtime conditions are three lists with different semantics.** `outcome_detectors` are terminal business results with a code the caller branches on (`MEMBER_NOT_FOUND`, `PERMISSION_DENIED`, `VALIDATION_ERROR`). `recoveries` are bounded, typed actions: `dismiss` (click a known control), `accept_dialog` (only messages matching a declared pattern), `retry` (rewind to the last verified state), `reauth` (jump to the sign-on step), each with `max_attempts`. Anything else that breaks a postcondition is a hard failure. A single free-text "expected errors" list would have collapsed exactly the distinction the brief calls out.
- **Cross-validation** rejects unknown param references, outputs that no step extracts, and a last step without a checkpoint.
- **Provenance** keeps model, run id, timestamp and the redacted goal. The transcript, observations and screenshots stay in the run directory.

## 3. Determinism & error handling

Replay walks the steps linearly. For each step: policy gate → wait for precondition → resolve locators → act → classify → checkpoints. There is no branching the artifact does not declare.

- **Waits are condition polls**, 100 ms interval, per-step timeout. The classifier waits until the postcondition *or* any detector *or* any recovery trigger holds, then evaluates in priority order: business outcome → recoverable → postcondition → hard failure. A "no member found" page is therefore reported immediately, not after a timeout. The only time-based wait is the retry backoff, and it is named as such.
- **Recoveries are bounded and rewinding is explicit.** `retry` goes back in history and rewinds to the step after the last verified postcondition, so credentials and form values are re-entered instead of re-clicking a dead page. `reauth` jumps to the entry step and continues linearly. `dismiss` re-acts only if the original action never happened. Attempts are counted per recovery across the run; exceeding the cap is a hard failure that names the recovery.
- **Hard failures are debuggable.** `Failure` carries the step id and label, what was expected (postcondition or checkpoint text), what was observed (url plus visible text per frame, or the locator attempts with match counts), and paths to a full-page screenshot and a redacted per-frame accessibility snapshot.
- **Business outcome vs failure vs recoverable** is encoded in `RunResult.status` and validated: `SUCCESS` carries outputs only, `BUSINESS_OUTCOME` needs an outcome code, `FAILED` needs failure detail, `ESCALATED` needs the intervention id.
- **Dialogs fail closed.** Unknown JavaScript dialogs are dismissed and recorded; only patterns declared in the artifact are accepted.
- **UI drift** is secondary by the brief's own framing; the locator ladder handles renamed or moved controls as long as the accessible name or the label text survives, and the attempt log tells a reviewer which rung failed.

## 4. Heterogeneity & multi-tenant

- **Surface seam.** `Surface` is a protocol: observe, resolve, act, check, wait_for, extract, screenshot, snapshot, dialog policy. The artifact only speaks roles, names, anchor text, visible text and coordinates. A desktop implementation over UI Automation or macOS AX would map the same vocabulary (a Windows control has a ControlType and a Name; anchors become the nearest preceding static text) and would implement the same protocol; the executor, the schema, the policy and the handoff do not change. A legacy web app with framesets is already the implemented case. What a desktop surface would lack is JS dialog interception; the same `DialogEvent` would come from window-open events instead.
- **Multi-tenant reuse.** Two layers: the artifact records the flow against a vendor product (`app.vendor/product/version`, `tenant: null` for a base artifact); the product profile (`app_profiles/<product>.yaml`) holds the detectors and recoveries every tenant of that product shares, and the recorder merges it into each artifact. A tenant that differs would override at three points without re-recording: `entry_url` (the tenant's origin, validated against policy), a tenant profile that replaces or extends the product profile, and per-step locator overrides keyed by step id. Accessible names and label text are the properties most likely to survive branding, which is why they lead the locator ladder and coordinates are last.
- **Drift detection** falls out of the run log: each replay records which locator strategy resolved each step. A step that starts resolving on its second or third rung on one tenant is drift on that tenant; a step that fails its checkpoint on all tenants is a version change. Aggregating that per tenant/version is the next step, not built.

## 5. Escalation & handoff

- **Detect.** Discovery: the model calls `escalate`, or the loop stops on max steps, timeout, or dead end (same action on an unchanged state hash three times). Replay: a hard failure, an exhausted recovery, or an irreversible step reaching the risk gate.
- **Route.** The executor calls an injected `EscalationHandler` with an `InterventionContext` (run, capability, step id and label, reason, kind, current url, screenshot and snapshot paths). The CLI handler writes `runs/<id>/intervention.json` with the same content plus the exact resume commands and prints it.
- **Control transfer.** One `ControlLease` per run, persisted as `runs/<id>/control.json`, with holder, state and a version counter. The state machine is AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION and rejects every other transition. While the human holds the lease automation only pumps the browser event loop and polls the lease file. The human drives the same headed Chromium session; nothing is reopened.
- **Human actions are recorded** by an in-page listener in every frame: clicks (role, name, tag), field changes (field identity and value length, never the value, nothing for password fields), submits and frame navigations. They go into the run log as `human_action` events and back to the caller in the `InterventionResult`.
- **Hand back.** `cuc resume --run-id <id> --decision resumed|confirmed|abandoned` writes the signal into the lease file from any terminal. On `resumed` the executor scans forward for the furthest step whose postcondition now holds (a human usually completes more than the failed step), logs which steps the human did, re-verifies, and continues. On `confirmed` it performs the gated irreversible step. On `abandoned` or timeout the run ends `ESCALATED`.

## 6. Safety

- **Allowlist** in `policy.yaml`: origins, glob routes with an explicit deny list, action types. An empty policy denies everything. Every model action and every replay step is checked before execution; the fault-injection routes are denied to the agent.
- **Risk classes.** Each step declares read / reversible / irreversible; policy takes the stricter of the declaration and a control-name heuristic ("Open Account", "Submit", "Transfer"…). Irreversible steps are never performed unattended: `require_human` routes them through the handoff for a `confirmed` decision; `block` refuses them outright. There is no allow mode.
- **Secrets by reference only.** Artifacts and params name environment variables. Values are resolved at act time into a `Secret` with a masked repr and registered with the redactor, so they are scrubbed from logs, results and accessibility snapshots even when they appear in page text (a typed user id does). The model types placeholders and never sees a value.
- **Redaction at the boundary.** One `Redactor` in front of the JSONL log, `result.json` and snapshot files: registered secrets, SSN and card patterns, 9–17 digit account numbers, bearer tokens, `key=value` secrets, and any payload key in the sensitive list.
- **Limits.** Screenshots cannot be redacted, so they are taken per action during discovery (local evidence) and only on failure or escalation during replay; whether to retain them is an operator decision. Outputs are financial values by design and become the caller's responsibility. The pattern list is a floor, not a classifier. The allowlist is URL-based; a desktop surface would need window/process allowlisting instead.

## 7. Cuts

Mocked or deliberately thin, each at a real seam:

- **Operator console is a CLI mock.** Instructions go to stdout and the resume signal is `cuc resume`. The lease, state machine, intervention request, same-session takeover and action capture are real. A web console would render `intervention.json` and write the same lease file.
- **Desktop surface not built.** The `Surface` protocol and the locator vocabulary are the seam; there is no UIA/AX implementation.
- **Per-tenant overrides are a design, not code.** `app.tenant` exists on the schema and product profiles are implemented; tenant profiles and per-step locator overrides are not.
- **Secret store is environment variables** behind `SecretStore`; no vault client.
- **Postcondition inference is a heuristic** (shortest new heading-like line). It is right on this app and is meant to be reviewed; the artifact is the review surface.
- **Detectors and recoveries come from the product profile**, not from discovery, because a happy-path run cannot observe them.
- **No assisted LLM fallback in replay**, on purpose: it would weaken the no-model guarantee that the import test enforces.
- **Human action capture is web-specific** and records identity plus value length; no replay of human actions.
- **No stability scoring, approval states, or code generation** (stretch goals not taken). The catalog is the one stretch goal built.

Next with more time: tenant profiles with locator overrides and a drift report from resolution-strategy statistics; an approval state (`draft` → `approved`) gating unattended replay of artifacts with irreversible steps; a UIA surface for one Windows app to prove the seam; screenshot redaction by masking control regions the a11y tree marks as sensitive.
