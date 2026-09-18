# Design report

## 1. Architecture

One Python process, no services, no queues. `schema` holds the contracts, `surface` perceives and acts, `replay` executes artifacts, `handoff` transfers control, `agent` discovers and records; dependencies point one way and `replay` never imports `agent` or a model SDK (a test parses the imports). Discovery runs an observe → decide → act loop with a fixed tool set and records a trace; the recorder turns it into an `Artifact`. Replay executes an artifact with typed params and no model and returns a `RunResult`; the catalog exposes artifacts as typed tools.

Perception is the accessibility tree, not pixels: each frame's ARIA snapshot plus an enumerated control list (role, accessible name, nearest preceding text, masked value). It is what a screen reader sees, it exists on desktop surfaces, and replay needs no vision model; canvas and image maps are a known blind spot. Playwright with Chromium supplies the tree across framesets, actionability checks, dialog interception, and one headed session a human can take over.

Discovery uses Groq's `openai/gpt-oss-120b` by default, with Gemini and Anthropic behind the same `LLMClient` protocol, bounded backoff on 429/5xx/timeouts, and a step cap. The target is a local, deliberately hostile app (frameset, nested tables, no ids, business errors as HTTP 200 pages) so runtime conditions can be injected rather than hoped for, with no real credentials or PII.

## 2. Artifact schema

The artifact is a contract an agent can call: identity and semver, `app` (vendor, product, version, tenant, entry url), typed `inputs` and `outputs`, `steps`, `checkpoints`, `outcome_detectors`, `recoveries`, `provenance`. JSON Schema is exported to `schema/`; a test fails when it is stale.

A step has an action, an ordered `locators` list with a rationale per locator, a `ValueRef`, a `risk_class` (read, reversible, irreversible), optional pre- and postconditions, a timeout, and for extract steps an `ExtractSpec`. Locators are `role_name`, `anchor` (nearest preceding visible text, the only stable handle in label-less legacy forms), `text`, and `coordinates` (last resort, refused if the viewport differs); replay stops at the first strategy yielding exactly one match, and more than one is a miss, never a guess.

Values are references (`literal`, `param`, or `secret`, which names an environment variable and rejects any value on it); sensitive inputs are passed as `secret:<ENV>`, so credentials cannot be serialized even by mistake.

Runtime conditions are three lists with different semantics: `outcome_detectors` are terminal business results with a code the caller branches on (`MEMBER_NOT_FOUND`, `PERMISSION_DENIED`); `recoveries` are bounded typed actions (`dismiss`, `accept_dialog` for declared patterns, `retry`, `reauth`) with `max_attempts`; anything else that breaks a postcondition is a hard failure. A single free-text "expected errors" list would have collapsed exactly this distinction. Cross-validation rejects unknown param references, outputs no step extracts, and a last step without a checkpoint.

## 3. Determinism & error handling

Replay walks the steps linearly (policy gate, precondition wait, locator resolution, action, classification, checkpoints) with no branching the artifact does not declare. Waits are condition polls under a per-step timeout: the classifier waits until the postcondition or any detector or recovery trigger holds, then evaluates business outcome, recoverable, postcondition, hard failure in that order, so a "no member found" page is reported immediately. The only time-based wait is the retry backoff.

Recoveries are bounded and rewinding is explicit: `retry` goes back in history and restarts after the last step with a verified postcondition, so form values are re-entered instead of re-clicking a dead page; `reauth` jumps to the entry step and continues linearly; `dismiss` re-acts only if the original action never happened. Exceeding a recovery's cap is a hard failure that names it.

A `Failure` carries the step, what was expected, what was observed (url plus visible text per frame, or locator attempts with match counts), and paths to a screenshot and a redacted accessibility snapshot; `RunResult` is validated per status. Unknown JavaScript dialogs are dismissed and recorded. UI drift is secondary here: the ladder tolerates renamed or moved controls while the accessible name or label text survives, and the attempt log shows which rung failed.

## 4. Heterogeneity & multi-tenant

`Surface` is a protocol (observe, resolve, act, check, wait, extract, screenshot, snapshot, dialog policy) and the artifact speaks only roles, names, anchor text, visible text and coordinates. A desktop implementation over UI Automation or macOS accessibility maps the same vocabulary (control type and name; anchors become the nearest preceding static text); the executor, schema, policy and handoff do not change.

For tenants, the artifact records the flow against a vendor product (`app.vendor/product/version`, `tenant: null` for a base artifact), and the product profile (`app_profiles/<product>.yaml`) holds the detectors and recoveries every tenant of that product shares. A tenant that differs overrides at three points without re-recording: `entry_url`, a tenant profile layered on the product profile, and per-step locator overrides keyed by step id. Accessible names and label text survive branding best, which is why they lead the ladder and coordinates are last.

Drift detection falls out of the run log, which records which locator rung resolved each step: a lower rung on one tenant is drift there, a checkpoint failing on all tenants is a version change. Aggregating that is designed, not built.

## 5. Escalation & handoff

Stuck is detected in discovery when the model calls `escalate` or the loop hits max steps, timeout or a dead end (the same action on an unchanged state hash three times), and in replay on any hard failure, exhausted recovery, or irreversible step at the risk gate. Every replay hard failure reaches an injected `EscalationHandler` with the run, capability, step, reason, url and evidence paths; the CLI handler writes that context and the exact resume commands to `runs/<id>/intervention.json`.

Control is a single `ControlLease` per run, persisted as `runs/<id>/control.json` with holder, state and version; the state machine AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION rejects every other transition. While the human holds the lease, automation only pumps the browser event loop and polls the lease file; the human drives the same headed Chromium session, and an in-page listener records clicks (role, name), field changes (identity and value length, never the value) and navigations.

`cuc resume --run-id <id> --decision resumed|confirmed|abandoned` writes the signal into the lease file from any terminal. On `resumed` the executor continues after the furthest step whose postcondition now holds and logs which steps the human did; on `confirmed` it performs the gated irreversible step; on `abandoned` or timeout the run ends `ESCALATED`.

## 6. Safety

`policy.yaml` allowlists origins, glob routes with an explicit deny list, and action types; an empty policy denies everything, and every model action and replay step is checked before execution.

Each step declares read, reversible or irreversible; policy takes the stricter of that and a control-name heuristic ("Open Account", "Transfer"). Irreversible steps are never performed unattended: `require_human` routes them through the handoff for a `confirmed` decision, `block` refuses them; there is no allow mode.

Secrets are references only, resolved at act time into a `Secret` with a masked repr and registered with the redactor, so they are scrubbed from logs, results and accessibility snapshots even when they appear in page text; the model types placeholders and never sees a value. One `Redactor` sits in front of every log and snapshot write, covering registered secrets, SSN, card and account-number patterns, bearer tokens, `key=value` secrets and sensitive payload keys.

Limits: screenshots cannot be redacted, so replay takes them only on failure or escalation; outputs are financial values by design; the pattern list is a floor; the allowlist is URL-based, so a desktop surface would need window or process allowlisting.

## 7. Cuts

The operator console is a CLI mock (stdout instructions, `cuc resume` as the signal); the lease, state machine, intervention request, same-session takeover and action capture are real. Not built: the desktop surface (the `Surface` protocol is the seam), tenant profiles and per-step locator overrides, a vault (environment variables behind `SecretStore`), assisted LLM fallback in replay (on purpose), replay of captured human actions, stability scoring, approval states and code generation; the catalog is the one stretch goal built. Postcondition inference is a heuristic (shortest new heading-like line) meant to be reviewed in the artifact, and detectors and recoveries come from the product profile because a happy-path run cannot observe them.

Next: tenant profiles with locator overrides and a drift report from resolution-strategy statistics; a `draft` → `approved` state gating unattended replay of artifacts with irreversible steps; a UI Automation surface for one Windows app; screenshot redaction by masking regions the accessibility tree marks sensitive.
