"""Discovery loop + recorder with a scripted fake LLM against the live app, then replay of the recorded artifact."""
from pathlib import Path

import pytest

from cuc.agent.llm import ModelTurn, ToolCall, ToolResult, ToolSpec
from cuc.agent.loop import AgentLoop
from cuc.agent.recorder import AppProfile, build_artifact, distinctive_text
from cuc.observability import RunLog
from cuc.policy import Policy, Redactor, SecretStore
from cuc.replay import InterventionResult, ReplayEngine
from cuc.schema import Artifact, LocatorStrategy, RiskClass, RunStatus

ROOT = Path(__file__).resolve().parents[1]
SECRETS = {"TARGET_APP_USER": "teller1", "TARGET_APP_PASSWORD": "Passw0rd!"}
SECRET_REFS = {"TARGET_APP_USER": "operator user id", "TARGET_APP_PASSWORD": "operator password"}


class FakeLLM:
    provider, model = "fake", "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.results: list[list[ToolResult]] = []
        self.prompts: list[str] = []

    def start(self, system, tools):
        assert all(isinstance(t, ToolSpec) for t in tools)

    def _next(self):
        if not self.script:
            return ModelTurn("out of script", [ToolCall("x", "escalate", {"reason": "script exhausted"})])
        name, args = self.script.pop(0)
        return ModelTurn("", [ToolCall(f"c{len(self.script)}", name, args)])

    def send_user(self, text):
        self.prompts.append(text)
        return self._next()

    def send_tool_results(self, results):
        self.results.append(results)
        return self._next()


HAPPY = [
    ("type_text", {"frame": "main", "role": "textbox", "anchor": "User ID", "text": "{{secret:TARGET_APP_USER}}", "reason": "sign on"}),
    ("type_text", {"frame": "main", "role": "textbox", "anchor": "Password", "text": "{{secret:TARGET_APP_PASSWORD}}", "reason": "sign on"}),
    ("click", {"frame": "main", "role": "button", "name": "Sign On", "reason": "submit sign on"}),
    ("type_text", {"frame": "main", "role": "textbox", "anchor": "Member Number", "text": "10023", "param_name": "member_id", "reason": "search"}),
    ("click", {"frame": "main", "role": "button", "name": "Search", "reason": "run search"}),
    ("extract", {"frame": "main", "output_name": "savings_balance", "strategy": "table_cell", "row_anchor": "Savings", "column_header": "Balance", "value_type": "money", "reason": "read balance"}),
    ("done", {"capability_id": "member_savings_balance_lookup", "description": "Look up a member and read the savings balance.", "summary": "balance read"}),
]


@pytest.fixture()
def make_loop(surface, live_server, tmp_path):
    def make(script, handler=None, **kw):
        redactor = Redactor({"password"})
        policy = Policy({"allow": {"origins": [live_server], "routes": ["/", "/nav", "/login", "/logout", "/members/**"],
                                   "actions": ["navigate", "click", "type", "select", "press", "extract"]},
                         "deny": {"routes": ["/admin/**"]},
                         "risk": {"irreversible_mode": "require_human", "irreversible_control_patterns": ["(?i)^open account"]}})
        run_dir = tmp_path / "disc"
        log = RunLog(run_dir, "disc-run", redactor)
        llm = FakeLLM(script)
        loop = AgentLoop(surface, llm, policy, log, SecretStore(redactor, env=SECRETS), run_dir, secret_refs=SECRET_REFS,
                         escalation=handler, screenshots=False, **kw)
        return loop, llm, log, redactor, policy
    return make


def test_discovery_records_artifact_that_replays(make_loop, surface, live_server, tmp_path):
    loop, llm, log, redactor, policy = make_loop(HAPPY)
    outcome = loop.run("look up member 10023 and read the savings balance", live_server + "/")
    assert outcome.status == "goal_met" and outcome.outputs == {"savings_balance": 1250.75}
    assert "Passw0rd!" not in log.path.read_text() and "teller1" not in log.path.read_text()
    assert "{{secret:TARGET_APP_USER}}" in llm.prompts[0]  # placeholders offered, values never shown to the model

    profile = AppProfile.load(ROOT / "app_profiles" / "core_serv.yaml")
    art = build_artifact(outcome, "look up member 10023 and read the savings balance", live_server + "/", profile, "disc-run", "fake:scripted", redactor)
    art = Artifact.model_validate_json(art.model_dump_json())  # serializable and valid
    assert [s.action.value for s in art.steps] == ["navigate", "type", "type", "click", "type", "click", "extract"]
    assert [p.name for p in art.inputs] == ["member_id"] and art.inputs[0].example == "10023"
    assert art.steps[1].value.kind == "secret" and art.steps[4].value.kind == "param"
    assert [l.strategy for l in art.steps[1].locators] == [LocatorStrategy.ANCHOR, LocatorStrategy.COORDINATES]
    assert [l.strategy for l in art.steps[3].locators] == [LocatorStrategy.ROLE_NAME, LocatorStrategy.TEXT, LocatorStrategy.COORDINATES]
    assert art.steps[3].postcondition.text == "Member Search" and art.steps[5].postcondition.text == "Member Profile"
    assert art.checkpoints[0].after_step == "s07" and art.checkpoints[0].condition.text == "Member Profile"
    assert {d.code for d in art.outcome_detectors} >= {"MEMBER_NOT_FOUND", "PERMISSION_DENIED"}
    assert next(r for r in art.recoveries if r.action == "reauth").goto_step == "s01"
    assert next(r for r in art.recoveries if r.action == "dismiss").locators[0].frame == "main"
    assert "Passw0rd!" not in art.model_dump_json() and "teller1" not in art.model_dump_json()

    for member, status, code in (("10047", RunStatus.SUCCESS, None), ("99999", RunStatus.BUSINESS_OUTCOME, "MEMBER_NOT_FOUND")):
        rlog = RunLog(tmp_path / f"replay-{member}", f"replay-{member}", redactor)
        r = ReplayEngine(surface, policy, rlog, SecretStore(redactor, env=SECRETS), tmp_path / f"replay-{member}").run(art, {"member_id": member})
        assert r.status is status, r
        if code:
            assert r.outcome.code == code
        else:
            assert r.outputs == {"savings_balance": 9800.0}


def test_policy_refuses_navigation_off_allowlist(make_loop, live_server):
    loop, llm, log, *_ = make_loop([("navigate", {"url": live_server + "/admin/fault/server_error", "reason": "cheat"}),
                                    ("navigate", {"url": "http://evil.example/", "reason": "cheat"})])
    outcome = loop.run("goal", live_server + "/")
    assert outcome.status == "escalated"  # script exhausted -> fake model escalates
    assert all(r[0].is_error and "refused" in r[0].content for r in llm.results[:2])
    assert not any(e.error is None and e.tool == "navigate" and "admin" in e.args["url"] for e in outcome.trace)


def test_irreversible_click_needs_human(make_loop, live_server):
    class Confirm:
        def request(self, ctx):
            assert ctx.kind == "confirm_irreversible"
            return InterventionResult("i1", "p", "confirmed")

    prefix = HAPPY[:5] + [("click", {"frame": "main", "role": "link", "name": "Open Sub-Account", "reason": "go to form"})]
    commit = ("click", {"frame": "main", "role": "button", "name": "Open Account", "reason": "commit"})
    loop, llm, *_ = make_loop(prefix + [commit])
    outcome = loop.run("open a sub-account for member 10023", live_server + "/")
    assert outcome.status == "escalated"
    refused = llm.results[-1][0]  # the commit click is the last call that got a result; the fake then escalates
    assert refused.is_error and "human confirmation" in refused.content
    loop, llm, *_ = make_loop(prefix + [commit, ("done", {"capability_id": "x", "description": "d", "summary": "s"})], handler=Confirm())
    outcome = loop.run("open a sub-account for member 10023", live_server + "/")
    assert outcome.status == "goal_met"
    last = [t for t in outcome.trace if t.tool == "click"][-1]
    assert last.human_confirmed and last.risk is RiskClass.IRREVERSIBLE


def test_dead_end_and_max_steps(make_loop, live_server):
    click = ("click", {"frame": "main", "role": "button", "name": "Sign On", "reason": "again"})
    loop, *_ = make_loop([click] * 6)
    assert loop.run("goal", live_server + "/").status == "dead_end"
    loop, *_ = make_loop(HAPPY, max_steps=2)
    assert loop.run("goal", live_server + "/").status == "max_steps"


def test_unknown_secret_and_bad_target_are_errors_not_actions(make_loop, live_server):
    loop, llm, *_ = make_loop([("type_text", {"frame": "main", "role": "textbox", "anchor": "User ID", "text": "{{secret:OTHER}}", "reason": "x"}),
                               ("click", {"frame": "main", "role": "button", "name": "Nope", "reason": "x"})])
    loop.run("goal", live_server + "/")
    assert "not available" in llm.results[0][0].content and "no locator resolved" in llm.results[1][0].content


def test_distinctive_text_prefers_headings_and_avoids_data():
    before = {"main": "Sign On\nUser ID\nPassword"}
    after = {"main": "Member Search\nStatus\tActive\nMember Number\n10023\n$1,250.75\nEnter the numeric member number exactly as printed on the member card."}
    assert distinctive_text(before, after, {"10023"}) == ("Member Search", "main")
    assert distinctive_text(after, after, set()) is None


def test_prune_trace_drops_failed_attempts_and_collapses_retyping():
    from cuc.agent.loop import TraceEntry
    from cuc.agent.recorder import AppProfile, prune_trace

    profile = AppProfile("v", "p", "1", outcome_detectors=[{"code": "INVALID_CREDENTIALS", "description": "", "condition": {"kind": "text_present", "text": "SEC-401"}}])
    tb = lambda i, tool, args, after: TraceEntry(i, tool, args, "", frame="main", text_after={"main": after})
    trace = [
        tb(0, "navigate", {"url": "u"}, "Sign On"),
        tb(1, "type_text", {"role": "textbox", "anchor": "User ID", "text": "{{secret:U}}"}, "Sign On"),
        tb(2, "click", {"role": "button", "name": "Sign On"}, "Invalid User ID or Password. (SEC-401)"),  # failed attempt ends here
        tb(3, "type_text", {"role": "textbox", "anchor": "Password", "text": "{{secret:P}}"}, "Sign On"),
        tb(4, "type_text", {"role": "textbox", "anchor": "User ID", "text": "{{secret:U}}"}, "Sign On"),
        tb(5, "type_text", {"role": "textbox", "anchor": "User ID", "text": "{{secret:U}}"}, "Sign On"),
        tb(6, "click", {"role": "button", "name": "Sign On"}, "Member Search"),
        tb(7, "extract", {"output_name": "x"}, "Member Search"),
    ]
    kept = prune_trace(trace, profile)
    assert [t.index for t in kept] == [0, 3, 5, 6, 7]
    assert prune_trace(trace[:1] + trace[3:], profile) == kept  # idempotent on an already-clean trace
