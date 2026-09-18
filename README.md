# cuc — computer-use capabilities for legacy back-office UIs

An LLM drives a legacy web app once to accomplish a goal. The successful run is recorded as a typed, versioned
capability artifact. Production invocations replay the artifact deterministically with no model in the loop and
return a structured result: success with outputs, a named business outcome, or a debuggable failure. When
automation cannot safely proceed, the live browser session is handed to a human and control comes back afterwards.
Design and trade-offs are in [REPORT.md](REPORT.md); per-module decisions in [DECISIONS.md](DECISIONS.md);
real-run evidence in [evidence/](evidence/README.md).

## Setup

Python 3.11 and Chromium via Playwright.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env    # fill in GROQ_API_KEY (or GEMINI_API_KEY / ANTHROPIC_API_KEY)
```

| `.env` key | Used by | Notes |
|---|---|---|
| `LLM_PROVIDER`, `LLM_MODEL` | `discover` only | `groq` + `openai/gpt-oss-120b` (default), `gemini` + `gemini-2.5-flash`, `anthropic` + `claude-opus-5` |
| `GROQ_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | `discover` only | replay never loads a model |
| `LLM_MAX_RETRIES`, `LLM_RETRY_BASE_SECONDS`, `LLM_REQUEST_TIMEOUT_S`, `CUC_MAX_STEPS` | `discover` | bounded backoff on 429/5xx/timeouts; step cap |
| `TARGET_APP_USER`, `TARGET_APP_PASSWORD` | replay + discover | referenced by name from artifacts, never stored in them |
| `CUC_HANDOFF_TIMEOUT_S` | handoff | how long automation waits for `cuc resume` |

Without live services: `pytest` runs 86 tests offline in about 2 minutes (in-process target app, headless Chromium,
scripted fake LLM). Replay never needs a model.

## Demo

Terminal A, keep running:

```bash
cuc target-app serve
```

Seeded operator `teller1` / `Passw0rd!`; `auditor1` has a read-only role. Members `10023`, `10047`, `10111`, `10500`.
Faults: `?fault=<kind>` on a request, or arm one for the next matching request via
`/admin/fault/<kind>?route=<prefix>&count=<n>` (kinds: `not_found`, `validation`, `permission_denied`,
`session_timeout`, `interstitial`, `confirm_dialog`, `slow`, `server_error`). The agent is denied `/admin/**` by policy.

Terminal B, repo root, venv active. Browser is headed by default; add `--headless` to any browser command.

**Discovery (real LLM run):**
```bash
cuc discover --goal "Look up member 10023 and read the current savings balance" --target http://127.0.0.1:5055/ \
  --secret TARGET_APP_USER="operator user id for sign on" --secret TARGET_APP_PASSWORD="operator password for sign on" \
  --capability-id member_savings_balance_lookup
```
Writes `artifacts/member_savings_balance_lookup.json` and `runs/<run-id>/events.jsonl` with a screenshot per action.
On a free-tier model prefix with `caffeinate -i`; `Retry-After` waits are honoured and each model call is timed in the log.

**Replay, success:**
```bash
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10047
```
**Replay, business outcome (not found):**
```bash
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=99999
```
**Replay, injected session timeout (recovered by re-authentication):**
```bash
curl -s "http://127.0.0.1:5055/admin/fault/session_timeout?route=/members/search&count=1"
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10023
```
**Replay, hard failure with evidence (persistent 500 exhausts the retry policy):**
```bash
curl -s "http://127.0.0.1:5055/admin/fault/server_error?route=/members&count=10"
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10023
curl -s "http://127.0.0.1:5055/admin/fault/clear"
```
**Handoff (human takes over the live session):**
```bash
curl -s "http://127.0.0.1:5055/admin/fault/server_error?route=/members&count=10"
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10023 --escalate --run-id handoff-demo
```
When `HUMAN INTERVENTION REQUIRED` prints, clear the fault, fix the session by hand in the browser window (sign on,
reach member search or the profile), then from another terminal:
```bash
curl -s "http://127.0.0.1:5055/admin/fault/clear"
cuc resume --run-id handoff-demo --decision resumed --note "fixed by hand"
```
Automation re-verifies the state and finishes. `runs/handoff-demo/` holds `intervention.json`, `control.json` (the lease)
and `events.jsonl` with `handoff_transition` and `human_action` records.

**Catalog (stretch):** `cuc catalog list --json`, `cuc catalog invoke member_savings_balance_lookup --param member_id=10023`.
**Other:** `cuc schema export` regenerates `schema/*.json`; `./scripts/secrets_scan.sh` before pushing.

## Result contract

`RunResult.status` is `SUCCESS` (with `outputs`), `BUSINESS_OUTCOME` (with `outcome.code`, e.g. `MEMBER_NOT_FOUND`),
`FAILED` (with `failure.step_id/expected/observed` and evidence paths) or `ESCALATED` (with the intervention id).
`recoveries_applied` lists dismissals, retries and re-authentications that happened on the way.

## Repo map

```
src/cuc/target_app/   legacy stand-in app (frameset, tables, no ids) + fault harness
src/cuc/schema/       Artifact and RunResult contracts; exported to schema/
src/cuc/surface/      Surface protocol + PlaywrightSurface (a11y perception, locator ladder)
src/cuc/replay/       deterministic executor; imports no LLM (tested)
src/cuc/policy/       allowlist, risk gate, redaction, secret references
src/cuc/agent/        LLM clients (groq/gemini/anthropic), tool set, loop, recorder
src/cuc/handoff/      control lease, state machine, intervention request, human capture, CLI handler (mock console)
src/cuc/catalog/      artifacts as typed tools
app_profiles/         product-level detectors/recoveries shared across tenants
policy.yaml           the allowlist in force
```
