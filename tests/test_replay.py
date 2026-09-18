"""Replay classification against the live target app: success, business outcome, recoveries, hard failure, escalation."""
import json
from urllib.request import urlopen

import pytest

from cuc.observability import RunLog
from cuc.policy import Policy, Redactor, SecretStore
from cuc.replay import InterventionResult, ReplayEngine
from cuc.schema import ActionType, Artifact, RiskClass, RunStatus, Step
from fixtures import loc_role, make_artifact, text_present

SECRETS = {"TARGET_APP_USER": "teller1", "TARGET_APP_PASSWORD": "Passw0rd!"}


def arm(base, kind, route="/members", count=1):
    urlopen(f"{base}/admin/fault/{kind}?route={route}&count={count}").read()


def clear(base):
    urlopen(f"{base}/admin/fault/clear").read()


class FakeHandler:
    def __init__(self, decision="abandoned", act=None):
        self.decision, self.act, self.calls = decision, act, []

    def request(self, ctx):
        self.calls.append(ctx)
        if self.act:
            self.act(ctx)
        return InterventionResult(intervention_id="int-1", request_path=str(ctx.run_dir / "intervention.json"), decision=self.decision)


@pytest.fixture()
def engine_factory(surface, live_server, tmp_path):
    clear(live_server)

    def make(handler=None, policy=None):
        redactor = Redactor({"password", "uid"})
        policy = policy or Policy({"allow": {"origins": [live_server], "routes": ["/", "/nav", "/login", "/logout", "/members/**"],
                                             "actions": ["navigate", "click", "type", "select", "press", "extract"]},
                                   "deny": {"routes": ["/admin/**"]},
                                   "risk": {"irreversible_mode": "require_human",
                                            "irreversible_control_patterns": ["(?i)^open account"]}})
        run_dir = tmp_path / "run"
        log = RunLog(run_dir, "test-run", redactor)
        return ReplayEngine(surface, policy, log, SecretStore(redactor, env=SECRETS), run_dir, escalation=handler), log

    yield make
    clear(live_server)


def events(log):
    return [json.loads(l) for l in log.path.read_text().splitlines()]


def test_success_returns_typed_outputs_and_redacted_log(engine_factory, live_server):
    engine, log = engine_factory()
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    assert r.status is RunStatus.SUCCESS and r.outputs == {"savings_balance": 1250.75} and r.steps_completed == 7
    raw = log.path.read_text()
    assert "Passw0rd!" not in raw and "teller1" not in raw
    kinds = [e["event"] for e in events(log)]
    assert kinds.count("step_completed") == 7 and "checkpoint" in kinds and kinds[-1] == "replay_finished"
    assert (log.run_dir / "result.json").exists()


def test_member_not_found_is_business_outcome_not_failure(engine_factory, live_server):
    engine, log = engine_factory()
    r = engine.run(make_artifact(live_server), {"member_id": "99999"})
    assert r.status is RunStatus.BUSINESS_OUTCOME and r.outcome.code == "MEMBER_NOT_FOUND" and r.outcome.step_id == "s06"
    assert r.failure is None and r.outputs == {}


def test_bad_params_rejected_before_touching_the_ui(engine_factory, live_server):
    engine, _ = engine_factory()
    with pytest.raises(ValueError, match="does not match"):
        engine.run(make_artifact(live_server), {"member_id": "abc"})


def test_session_timeout_is_recovered_by_reauth(engine_factory, live_server):
    engine, log = engine_factory()
    arm(live_server, "session_timeout", route="/members/search")
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    assert r.status is RunStatus.SUCCESS
    assert [a.recovery_id for a in r.recoveries_applied] == ["r02"]
    started = [e["step_id"] for e in events(log) if e["event"] == "step_started"]
    assert started.count("s01") == 2 and started[-1] == "s07"  # jumped back to sign-on, then finished linearly


def test_transient_server_error_is_retried_from_last_verified_state(engine_factory, live_server):
    engine, log = engine_factory()
    arm(live_server, "server_error", route="/members/search", count=1)  # fires on the post-sign-on redirect
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    assert r.status is RunStatus.SUCCESS and [a.recovery_id for a in r.recoveries_applied] == ["r03"]
    started = [e["step_id"] for e in events(log) if e["event"] == "step_started"]
    # s04 (sign on) failed with HTTP 500; the engine went back and rewound to just after s01, the last step
    # with a verified postcondition, so credentials were re-entered rather than blindly re-clicking.
    assert started.count("s01") == 1 and started.count("s02") == 2 and started.count("s04") == 2


def test_persistent_server_error_is_a_hard_failure_with_evidence(engine_factory, live_server):
    engine, log = engine_factory()
    arm(live_server, "server_error", route="/members", count=10)
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    clear(live_server)
    assert r.status is RunStatus.FAILED
    assert "r03" in r.failure.expected and "HTTP 500" in r.failure.observed
    assert r.failure.evidence.screenshot and r.failure.evidence.a11y_snapshot
    assert len(r.recoveries_applied) == 2


def test_interstitial_is_dismissed_and_flow_continues(engine_factory, live_server):
    engine, _ = engine_factory()
    arm(live_server, "interstitial", route="/members/10023")
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    assert r.status is RunStatus.SUCCESS and [a.recovery_id for a in r.recoveries_applied] == ["r01"]


def test_permission_denied_is_business_outcome(engine_factory, live_server):
    engine, _ = engine_factory()
    arm(live_server, "permission_denied", route="/members/10023")
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    assert r.status is RunStatus.BUSINESS_OUTCOME and r.outcome.code == "PERMISSION_DENIED"


def test_policy_denied_entry_url(engine_factory, live_server):
    engine, _ = engine_factory(policy=Policy({}))
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    assert r.status is RunStatus.FAILED and "origin" in r.failure.observed and r.failure.step_id is None


def _with_irreversible_step(base):
    a = make_artifact(base, include_extract=False)
    steps = a.steps + [
        Step(id="s07", label="Open sub-account form", action=ActionType.CLICK, locators=[loc_role("link", "Open Sub-Account")],
             postcondition=text_present("Open Sub-Account")),
        Step(id="s08", label="Commit the new sub-account", action=ActionType.CLICK, locators=[loc_role("button", "Open Account")],
             risk_class=RiskClass.IRREVERSIBLE, postcondition=text_present("Sub-Account Opened")),
    ]
    cps = [cp for cp in a.checkpoints if cp.after_step != "s06"] + [a.checkpoints[-1].model_copy(update={"after_step": "s08", "condition": text_present("Sub-Account Opened")})]
    return a.model_copy(update={"steps": steps, "checkpoints": cps})


def test_irreversible_step_without_handler_fails_closed(engine_factory, live_server):
    engine, log = engine_factory()
    r = engine.run(_with_irreversible_step(live_server), {"member_id": "10047"})
    assert r.status is RunStatus.FAILED and r.failure.step_id == "s08" and "human confirmation" in r.failure.observed
    assert not any(e["event"] == "acted" and e["step_id"] == "s08" for e in events(log))


def test_irreversible_step_abandoned_by_human_is_escalated(engine_factory, live_server):
    h = FakeHandler("abandoned")
    engine, _ = engine_factory(handler=h)
    r = engine.run(_with_irreversible_step(live_server), {"member_id": "10047"})
    assert r.status is RunStatus.ESCALATED and r.escalation.step_id == "s08" and h.calls[0].kind == "confirm_irreversible"


def test_irreversible_step_confirmed_by_human_proceeds(engine_factory, live_server):
    h = FakeHandler("confirmed")
    engine, _ = engine_factory(handler=h)
    art = _with_irreversible_step(live_server)
    r = engine.run(art, {"member_id": "10047"})
    # The form is submitted empty so the app's validation rejects it: a hard failure at s08 is the honest result here.
    assert r.status is RunStatus.FAILED and r.failure.step_id == "s08" and h.calls[0].kind == "confirm_irreversible"
    assert "ERROR" in r.failure.observed


def test_stuck_replay_escalates_then_resumes_when_human_fixed_it(engine_factory, live_server, surface):
    def human_fixes(ctx):  # the operator signs on and reaches the member profile in the same browser
        from conftest import login_via_surface
        from fixtures import loc_anchor
        login_via_surface(surface, live_server)
        surface.act(ActionType.TYPE, surface.resolve([loc_anchor("textbox", "Member Number")], 3000), "10023", 3000)
        surface.act(ActionType.CLICK, surface.resolve([loc_role("button", "Search")], 3000), None, 3000)
        surface.wait_for(text_present("Member Profile"), 3000)

    h = FakeHandler("resumed", act=human_fixes)
    engine, log = engine_factory(handler=h)
    art = make_artifact(live_server)
    art = art.model_copy(update={"recoveries": [r for r in art.recoveries if r.id != "r03"]})  # no retry policy: 500 is stuck
    arm(live_server, "server_error", route="/members/search", count=1)
    r = engine.run(art, {"member_id": "10023"})
    assert r.status is RunStatus.SUCCESS and r.outputs == {"savings_balance": 1250.75}
    assert h.calls[0].kind == "stuck" and h.calls[0].step_id == "s04" and h.calls[0].evidence.screenshot  # 500 hit the post-sign-on redirect
    assert any(e["event"] == "resume_verified" for e in events(log))


def test_exhausted_recovery_escalates_when_a_handler_exists(engine_factory, live_server):
    h = FakeHandler("abandoned")
    engine, log = engine_factory(handler=h)
    arm(live_server, "server_error", route="/members", count=10)
    r = engine.run(make_artifact(live_server), {"member_id": "10023"})
    clear(live_server)
    assert r.status is RunStatus.ESCALATED and r.escalation.step_id == "s04" and "r03" in r.escalation.reason
    assert h.calls[0].kind == "stuck" and h.calls[0].evidence.screenshot
