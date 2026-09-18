import json

import pytest

from cuc.handoff import CliEscalationHandler, HandoffMachine, HandoffState, IllegalTransition, LeaseFile
from cuc.observability import RunLog
from cuc.policy import Redactor
from cuc.replay.executor import InterventionContext
from cuc.schema import EvidenceRefs
from conftest import login_via_surface
from fixtures import text_present


def test_state_machine_transitions_and_lease_persistence(tmp_path):
    seen = []
    m = HandoffMachine("run-1", tmp_path, on_transition=lambda f, t, r: seen.append((f.value, t.value)))
    assert m.state is HandoffState.AUTOMATION and LeaseFile(tmp_path).read().holder == "automation"
    with pytest.raises(IllegalTransition):
        m.grant_to_human()  # must pause first
    m.pause("int-1", "stuck")
    with pytest.raises(IllegalTransition):
        m.begin_resume()
    m.grant_to_human()
    lease = LeaseFile(tmp_path).read()  # a second process sees who is in control
    assert lease.state is HandoffState.HUMAN and lease.holder == "human" and lease.intervention_id == "int-1" and lease.version == 2
    with pytest.raises(IllegalTransition):
        m.pause("int-2", "again")
    m.begin_resume()
    m.resume_complete()
    assert seen == [("AUTOMATION", "PAUSED"), ("PAUSED", "HUMAN"), ("HUMAN", "RESUMING"), ("RESUMING", "AUTOMATION")]


def test_resume_signal_only_while_human_holds_lease(tmp_path):
    m = HandoffMachine("run-1", tmp_path)
    lf = LeaseFile(tmp_path)
    with pytest.raises(RuntimeError, match="not HUMAN"):
        lf.signal_resume("resumed")
    m.pause("int-1", "x")
    m.grant_to_human()
    with pytest.raises(ValueError):
        lf.signal_resume("maybe")
    lf.signal_resume("resumed", note="re-signed on")
    assert m.poll_resume()["note"] == "re-signed on"
    m.begin_resume()
    assert m.poll_resume() is None  # signal is consumed by leaving HUMAN


def test_cli_handler_transfers_control_records_human_and_resumes(surface, live_server, tmp_path):
    login_via_surface(surface, live_server)
    log = RunLog(tmp_path, "run-h", Redactor({"password"}))
    handler = CliEscalationHandler(surface, log, tmp_path, timeout_s=20, poll_ms=100, announce=lambda s: None)
    ctx = InterventionContext(run_id="run-h", run_dir=tmp_path, capability_id="cap", description="d", step_id="s05", step_label="Enter member",
                              reason="stuck", kind="stuck", evidence=EvidenceRefs(), current_url=surface.current_url(), steps_completed=4)

    def human():  # a human drives the SAME session: types a member number and searches, then signals resume from "another terminal"
        page = surface.page
        page.wait_for_timeout(300)
        main = next(f for f in page.frames if f.name == "main")
        main.locator("input[name=member_number]").fill("10023")
        main.locator("input[name=member_number]").dispatch_event("change")
        main.get_by_role("button", name="Search").click()
        page.wait_for_timeout(500)
        LeaseFile(tmp_path).signal_resume("resumed", note="searched manually")

    # Playwright's sync API is single-threaded; simulate the human on the main thread after the handler starts polling
    # by scheduling the actions inside the poll loop via a one-shot page task.
    scheduled = {"done": False}
    orig_wait = surface.page.wait_for_timeout

    def wait_and_act(ms):
        orig_wait(ms)
        if not scheduled["done"]:
            scheduled["done"] = True
            human()

    surface.page.wait_for_timeout = wait_and_act
    try:
        res = handler.request(ctx)
    finally:
        surface.page.wait_for_timeout = orig_wait
    assert res.decision == "resumed" and res.intervention_id.startswith("int-")
    kinds = [e["kind"] for e in res.human_actions]
    assert "change" in kinds and "click" in kinds and "navigated" in kinds
    change = next(e for e in res.human_actions if e["kind"] == "change")
    assert change["value_length"] == 5 and "10023" not in json.dumps(change)  # identity and length only, never the value
    req = json.loads((tmp_path / "intervention.json").read_text())
    assert req["step_id"] == "s05" and "cuc resume --run-id run-h" in req["how_to_resume"]
    events = [json.loads(l) for l in log.path.read_text().splitlines()]
    states = [(e["from"], e["to"]) for e in events if e["event"] == "handoff_transition"]
    assert states == [("AUTOMATION", "PAUSED"), ("PAUSED", "HUMAN"), ("HUMAN", "RESUMING"), ("RESUMING", "AUTOMATION")]
    assert any(e["event"] == "human_action" for e in events)
    assert surface.check(text_present("Member Profile"))  # the human's work is on the live session automation resumes on


def test_cli_handler_times_out_to_abandoned(surface, live_server, tmp_path):
    login_via_surface(surface, live_server)
    log = RunLog(tmp_path, "run-t", Redactor())
    handler = CliEscalationHandler(surface, log, tmp_path, timeout_s=0.3, poll_ms=50, announce=lambda s: None)
    ctx = InterventionContext(run_id="run-t", run_dir=tmp_path, capability_id="cap", description="d", step_id="s01", step_label="x",
                              reason="r", kind="stuck", evidence=EvidenceRefs(), current_url="", steps_completed=0)
    assert handler.request(ctx).decision == "abandoned"
    assert LeaseFile(tmp_path).read().state is HandoffState.AUTOMATION
