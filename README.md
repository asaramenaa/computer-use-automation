# cuc — computer-use capabilities for legacy back-office UIs

An LLM discovers how to accomplish a goal in a legacy web app once. The successful run is recorded as a
typed, versioned **capability artifact**. Production invocations **replay** that artifact deterministically,
with no model in the loop, and return a structured result (success with outputs, a named business outcome,
or a debuggable failure). When automation cannot safely proceed, the live browser session is **handed to a
human** and control comes back afterwards.

```
goal ──► discover (LLM, once) ──► artifacts/<capability>.json ──► replay (no LLM, many) ──► RunResult
                                        │                              │
                                  reviewable contract          escalate ⇄ human on the same session
```

Design write-up: [REPORT.md](REPORT.md). Per-module decisions: [DECISIONS.md](DECISIONS.md).
JSON Schema for the contracts: [schema/](schema/). Evidence from real runs: [evidence/](evidence/).

## Layout

| Path | What |
|---|---|
| `src/cuc/target_app/` | Local stand-in for a legacy member-servicing app: frameset, nested tables, no ids/labels. Fault injection harness. |
| `src/cuc/schema/` | Pydantic contracts: `Artifact` (the capability) and `RunResult` (the replay result). Exported to `schema/`. |
| `src/cuc/surface/` | `Surface` protocol (perceive/act seam) + `PlaywrightSurface`: accessibility-tree perception, ordered locator resolution. |
| `src/cuc/replay/` | Deterministic executor: business outcome → recoverable → postcondition → hard failure. Imports no LLM (tested). |
| `src/cuc/policy/` | YAML allowlist, risk gating (irreversible needs a human), redaction at the log boundary, secrets by reference. |
| `src/cuc/agent/` | Discovery loop (Groq, Gemini or Anthropic behind one `LLMClient`, function calling), fixed tool set, recorder trace → artifact. |
| `src/cuc/handoff/` | Control lease + state machine, intervention request, human action capture, CLI operator handler (mock console). |
| `src/cuc/catalog/` | Artifacts as typed tools; invoke by name (stretch goal). |
| `src/cuc/observability/` | JSONL run log, screenshot + accessibility snapshot evidence. |
| `app_profiles/` | Product-level detectors/recoveries shared by every tenant of a vendor product. |
| `policy.yaml` | The allowlist and risk policy in force. |

## Setup

Requirements: Python 3.11, Chromium via Playwright.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env        # then fill in GROQ_API_KEY (or GEMINI_API_KEY / ANTHROPIC_API_KEY)
```

`.env` keys (see `.env.example`):

| Key | Used by | Notes |
|---|---|---|
| `LLM_PROVIDER`, `LLM_MODEL` | `discover` only | `groq` + `openai/gpt-oss-120b` (default), `gemini` + `gemini-2.5-flash`, or `anthropic` + `claude-opus-5` |
| `GROQ_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | `discover` only | replay never loads a model |
| `LLM_MAX_RETRIES`, `LLM_RETRY_BASE_SECONDS`, `CUC_MAX_STEPS` | `discover` | 429 backoff cap and step cap so a free-tier quota is not burned |
| `TARGET_APP_USER`, `TARGET_APP_PASSWORD` | replay + discover | referenced by name from artifacts, never stored in them |
| `CUC_HANDOFF_TIMEOUT_S` | handoff | how long automation waits for `cuc resume` |

**Running without live services:** the whole test suite (`pytest`) runs offline. It starts the target app in-process,
drives a headless Chromium, and uses a scripted fake LLM for the discovery loop. Replay never needs a model.

## Run the target app

```bash
cuc target-app serve
```

Serves `http://127.0.0.1:5055/`. Seeded fake operators: `teller1` / `Passw0rd!` (teller) and `auditor1` / `Passw0rd!`
(read-only role, so opening a sub-account is denied). Seeded members: `10023`, `10047`, `10111`, `10500`.

Fault injection (test harness only, denied to the agent by policy): `?fault=<kind>` on any request, or arm a
process-wide fault for the next matching request:

```bash
curl "http://127.0.0.1:5055/admin/fault/session_timeout?route=/members&count=1"
```

Kinds: `not_found`, `validation`, `permission_denied`, `session_timeout`, `interstitial`, `confirm_dialog`, `slow`, `server_error`.

## Demo path

All commands from the repo root with the venv active and the target app running in another terminal.

**1. Discovery (real LLM run, headed browser):**

```bash
cuc discover \
  --goal "Look up member 10023 and read the current savings balance" \
  --target http://127.0.0.1:5055/ \
  --secret TARGET_APP_USER="operator user id for sign on" \
  --secret TARGET_APP_PASSWORD="operator password for sign on" \
  --capability-id member_savings_balance_lookup
```

Writes `artifacts/member_savings_balance_lookup.json` and `runs/discover-<ts>/events.jsonl` with a screenshot per action.

On a free-tier model the run can take a while because of `Retry-After` waits; on a laptop, prefix the command with
`caffeinate -i` so the machine does not sleep mid-run. Every model call is logged with its elapsed time (`llm_call`)
so slow runs can be attributed.

**2. Replay, success:**

```bash
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10047
```

**3. Replay, business outcome (member not found):**

```bash
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=99999
```

**4. Replay, injected session timeout (recovered by re-authentication):**

```bash
curl -s "http://127.0.0.1:5055/admin/fault/session_timeout?route=/members/search&count=1"
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10023
```

**5. Replay, hard failure with evidence (persistent HTTP 500 exhausts the retry policy):**

```bash
curl -s "http://127.0.0.1:5055/admin/fault/server_error?route=/members&count=10"
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10023
curl -s "http://127.0.0.1:5055/admin/fault/clear"
```

**6. Handoff run (a human takes over the live session):**

```bash
# terminal A: arm a 500 that the artifact cannot recover from, then replay with escalation enabled
curl -s "http://127.0.0.1:5055/admin/fault/server_error?route=/members&count=10"
cuc replay --artifact artifacts/member_savings_balance_lookup.json --param member_id=10023 --escalate --run-id handoff-demo
```
When it prints `HUMAN INTERVENTION REQUIRED`, clear the fault and fix the session by hand in the browser window
(sign on again if needed, search the member, reach the profile), then from terminal B:
```bash
curl -s "http://127.0.0.1:5055/admin/fault/clear"
cuc resume --run-id handoff-demo --decision resumed --note "cleared the outage and reached the profile manually"
```
Automation re-verifies the state, continues, and returns SUCCESS. `runs/handoff-demo/` holds `intervention.json`,
`control.json` (the lease), and `events.jsonl` with `handoff_transition` and `human_action` records.

**7. Catalog (stretch):**

```bash
cuc catalog list --json
cuc catalog invoke member_savings_balance_lookup --param member_id=10023
```

**Other:** `cuc schema export` regenerates `schema/*.json`; `--headless` on any browser command; `pytest` runs 86 tests in about 2 minutes.

## Result contract (what a calling agent gets back)

`RunResult.status` is one of `SUCCESS` (with `outputs`), `BUSINESS_OUTCOME` (with `outcome.code`, e.g. `MEMBER_NOT_FOUND`),
`FAILED` (with `failure.step_id/expected/observed` and evidence paths), or `ESCALATED` (with the intervention id).
`recoveries_applied` lists any interstitial dismissals, retries or re-authentication that happened on the way.

## Evidence

`evidence/` is filled only by real runs; see `evidence/README.md` for what each file is once the runs are in.
