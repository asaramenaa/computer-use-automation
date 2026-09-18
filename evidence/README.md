# Evidence

Everything here was produced by real runs of the code in this repository against the local target app.
Nothing was edited by hand. Run logs are JSONL written through the redactor; credentials never appear
(the operator user id and password are `secret:` references resolved from the environment at act time).

## artifact/

`member_savings_balance_lookup.json` — the capability recorded by the successful discovery run. 7 steps,
1 typed input (`member_id`, pattern `^\d+$`), 1 typed output (`savings_balance`, money), a success checkpoint,
4 business-outcome detectors and 4 recoveries merged from `app_profiles/core_serv.yaml`. Provenance names the
model (`groq:openai/gpt-oss-120b`) and the run id.

## discovery/

The successful LLM-driven run (`run_id: discover-real`, 2026-09-18, Groq `openai/gpt-oss-120b`).

- `events.jsonl` — every model decision with the model's own reason (`agent_decision`), each model call's elapsed
  time, attempt count and token usage (`llm_call`), the resolved locator strategy per action (`agent_step`), and
  `artifact_written`. 7 actions, 8 model calls, ~19.5k input / ~0.6k output tokens.
- `console.txt` — CLI output including the `Retry-After` waits on the free tier.
- `screenshots/` — one full-page screenshot per action. Screenshots are not redacted: the sign-on screenshot shows the
  seeded demo user id (`teller1`, published in the README) in the field; the password field is masked by the browser.

## discovery-failed-attempts/

The four earlier discovery runs, kept because each changed the code (see DECISIONS.md):

1. `attempt1-groq-413-request-too-large` — the free tier rejects requests over 8,000 tokens; the resent conversation
   grew past it on the last turn. Fix: earlier observations are elided so request size stays roughly constant.
2. `attempt2-connection-error` — a hung request became a fatal connection error. Fix: 120 s request timeout, connection
   errors and 5xx retried under the same bounded backoff as 429.
3. `attempt3-goal-met-but-failed-signon-recorded` (+ `attempt3-artifact-unpruned.json`) — the goal was met but the
   system prompt's example placeholder name did not match the offered ones; the model typed it, sign-on failed, and the
   recorder kept the failed attempt (step s03 "expects" the SEC-401 page). Fix: prompt names only the offered
   placeholders; the recorder prunes actions up to the last terminal business outcome and collapses re-typing.
4. `attempt4-groq-400-tool-arg-null` — Groq validates tool arguments server-side and rejected `"name": null`. Fix:
   optional tool fields are nullable; a validation rejection is fed back to the model and retried at most twice.

## replay-* (deterministic, no model loaded)

Each folder has `events.jsonl`, `result.json` (the `RunResult` contract) and `console.txt`.

| Folder | Params / injected condition | Result |
|---|---|---|
| `replay-success` | `member_id=10047` | `SUCCESS`, `outputs.savings_balance = 9800.0` |
| `replay-not-found` | `member_id=99999` | `BUSINESS_OUTCOME`, `outcome.code = MEMBER_NOT_FOUND` at step s06, no failure |
| `replay-session-timeout` | `member_id=10023`, `session_timeout` armed on `/members/search` | `SUCCESS`, `recoveries_applied = [r02]` (re-authenticated from s01 and continued) |
| `replay-server-error` | `member_id=10023`, `server_error` armed for 10 requests on `/members` | `FAILED` at s04 after `r03` retried twice; `failure.expected/observed` plus `001_s04_hard_failure.png` and the redacted accessibility snapshot `001_s04_hard_failure.a11y.txt` |

## handoff-demo/ (human takes over the live session)

Same artifact, `member_id=10023`, `server_error` armed for 10 requests, replay started with `--escalate`.

- `events.jsonl` — `r03` retried twice, then `escalation_requested` at s04; `handoff_transition` AUTOMATION → PAUSED → HUMAN
  (lease v2), 13 `human_action` records while the operator held the lease (clicks by role/name, field changes as identity plus
  value length: the user id shows `value_length: 7`, the password field `null`, never a value), `human_resume_signal`
  (`decision: resumed`, written by `cuc resume` from another terminal), HUMAN → RESUMING → AUTOMATION (lease v4),
  `resume_verified` (the engine found s04's postcondition holding and continued after it), then `replay_finished`.
- `intervention.json` — what the operator was given: capability, step, reason, current url, screenshot path, resume commands.
- `control.json` — the lease at the end of the run: holder automation, version 4, with the operator's resume note.
- `result.json` — `SUCCESS`, `outputs.savings_balance = 1250.75`, `recoveries_applied = [r03, r03]`.
- `001_s04_hard_failure.png` / `.a11y.txt` — the evidence captured at the moment of escalation.

Note what the human actually did: signed on and reached the member search screen, then signalled resume. Automation
verified that state, typed the member number, searched and extracted the balance itself.
