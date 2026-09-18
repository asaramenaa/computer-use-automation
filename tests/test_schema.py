import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cuc.schema import (
    ActionType, Artifact, InputParam, Locator, LocatorStrategy, Recovery,
    RecoveryAction, RunResult, RunStatus, Step, ValueRef, Failure, Outcome,
)
from cuc.schema.export import render
from fixtures import make_artifact, loc_role, text_present

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_artifact_round_trips():
    a = make_artifact()
    dumped = a.model_dump_json()
    assert Artifact.model_validate_json(dumped) == a
    assert "Passw0rd" not in dumped


def test_locator_strategy_requires_fields():
    with pytest.raises(ValidationError):
        Locator(strategy=LocatorStrategy.ROLE_NAME, role="button", rationale="x")
    with pytest.raises(ValidationError):
        Locator(strategy=LocatorStrategy.COORDINATES, x=1, y=2, rationale="x")
    Locator(strategy=LocatorStrategy.COORDINATES, x=1, y=2, viewport=(1280, 800), rationale="last resort")


def test_value_ref_secret_never_carries_value():
    with pytest.raises(ValidationError):
        ValueRef(kind="secret", name="APP_PASSWORD", value="hunter2")
    with pytest.raises(ValidationError):
        ValueRef(kind="secret", name="lowercase")


def test_step_shape_rules():
    with pytest.raises(ValidationError):
        Step(id="s01", label="x", action=ActionType.CLICK)  # no locator
    with pytest.raises(ValidationError):
        Step(id="s01", label="x", action=ActionType.TYPE, locators=[loc_role("textbox", "a")])  # no value


def test_cross_reference_validation():
    a = make_artifact()
    bad = a.model_dump()
    bad["steps"][4]["value"] = {"kind": "param", "name": "nope"}
    with pytest.raises(ValidationError, match="unknown param"):
        Artifact.model_validate(bad)
    bad = a.model_dump()
    bad["outputs"] = []
    with pytest.raises(ValidationError, match="outputs"):
        Artifact.model_validate(bad)
    bad = a.model_dump()
    bad["checkpoints"] = bad["checkpoints"][:1]
    with pytest.raises(ValidationError, match="last step"):
        Artifact.model_validate(bad)


def test_sensitive_input_rules():
    with pytest.raises(ValidationError):
        InputParam(name="ssn", sensitive=True, example="123-45-6789")
    a = make_artifact()
    a2 = a.model_copy(update={"inputs": a.inputs + [InputParam(name="pin", sensitive=True, required=False)]})
    assert a2.input_json_schema()["properties"]["pin"]["pattern"].startswith("^secret:")
    with pytest.raises(ValueError, match="secret:"):
        a2.validate_params({"member_id": "10023", "pin": "1234"})
    assert a2.validate_params({"member_id": "10023", "pin": "secret:MEMBER_PIN"})["pin"] == "secret:MEMBER_PIN"


def test_validate_params_pattern_and_unknown():
    a = make_artifact()
    with pytest.raises(ValueError, match="does not match"):
        a.validate_params({"member_id": "abc"})
    with pytest.raises(ValueError, match="unknown"):
        a.validate_params({"member_id": "10023", "extra": 1})
    with pytest.raises(ValueError, match="missing"):
        a.validate_params({})
    assert a.validate_params({"member_id": 10023}) == {"member_id": "10023"}


def test_recovery_rules():
    with pytest.raises(ValidationError):
        Recovery(id="r01", description="x", trigger=text_present("x"), action=RecoveryAction.DISMISS)
    with pytest.raises(ValidationError):
        Recovery(id="r01", description="x", trigger=text_present("x"), action=RecoveryAction.ACCEPT_DIALOG)


def test_run_result_consistency():
    kw = dict(run_id="r", capability_id="c", version="1.0.0", started_at="2026-01-01T00:00:00Z",
              finished_at="2026-01-01T00:00:01Z", evidence_dir="runs/r")
    with pytest.raises(ValidationError):
        RunResult(status=RunStatus.FAILED, **kw)
    with pytest.raises(ValidationError):
        RunResult(status=RunStatus.SUCCESS, outcome=Outcome(code="X", description="", step_id="s01"), **kw)
    r = RunResult(status=RunStatus.FAILED, failure=Failure(step_id="s06", step_label="Search", expected="Member Profile", observed="HTTP 500"), **kw)
    assert json.loads(r.model_dump_json())["failure"]["expected"] == "Member Profile"


def test_exported_json_schema_is_current():
    for fname, text in render().items():
        on_disk = (ROOT / "schema" / fname)
        assert on_disk.exists(), f"run `cuc schema export` to create {fname}"
        assert on_disk.read_text() == text, f"{fname} is stale; run `cuc schema export`"
