"""Hand-built artifact fixture for the seeded target app. Used by schema, replay and catalog tests."""
from datetime import datetime, timezone

from cuc.schema import (
    ActionType, AppIdentity, Artifact, Checkpoint, Condition, ConditionKind, ExtractSpec, ExtractStrategy,
    InputParam, Locator, LocatorStrategy, OutcomeDetector, OutputField, Provenance, Recovery, RecoveryAction,
    RiskClass, Step, ValueRef,
)


def loc_role(role, name, frame="main", rationale="accessible name is stable"):
    return Locator(strategy=LocatorStrategy.ROLE_NAME, frame=frame, role=role, name=name, rationale=rationale)


def loc_anchor(role, anchor, frame="main", rationale="control is unlabeled; adjacent cell text is the only stable handle"):
    return Locator(strategy=LocatorStrategy.ANCHOR, frame=frame, role=role, anchor_text=anchor, rationale=rationale)


def text_present(text, frame="main"):
    return Condition(kind=ConditionKind.TEXT_PRESENT, frame=frame, text=text)


def make_artifact(base_url="http://127.0.0.1:5055", include_extract=True) -> Artifact:
    steps = [
        Step(id="s01", label="Open the application", action=ActionType.NAVIGATE,
             value=ValueRef(kind="literal", value=base_url + "/"),
             postcondition=Condition(kind=ConditionKind.ELEMENT_PRESENT, frame="main", locator=loc_role("button", "Sign On"))),
        Step(id="s02", label="Enter operator user id", action=ActionType.TYPE,
             locators=[loc_anchor("textbox", "User ID")], value=ValueRef(kind="secret", name="TARGET_APP_USER")),
        Step(id="s03", label="Enter operator password", action=ActionType.TYPE,
             locators=[loc_anchor("textbox", "Password")], value=ValueRef(kind="secret", name="TARGET_APP_PASSWORD")),
        Step(id="s04", label="Sign on", action=ActionType.CLICK, locators=[loc_role("button", "Sign On")],
             postcondition=text_present("Member Search")),
        Step(id="s05", label="Enter member number", action=ActionType.TYPE,
             locators=[loc_anchor("textbox", "Member Number")], value=ValueRef(kind="param", name="member_id")),
        Step(id="s06", label="Search", action=ActionType.CLICK, locators=[loc_role("button", "Search")],
             postcondition=text_present("Member Profile")),
    ]
    outputs = []
    if include_extract:
        steps.append(Step(id="s07", label="Read savings balance", action=ActionType.EXTRACT, risk_class=RiskClass.READ,
                          extract=ExtractSpec(output="savings_balance", strategy=ExtractStrategy.TABLE_CELL, frame="main",
                                              row_anchor="Savings", column_header="Balance", parse="money")))
        outputs.append(OutputField(name="savings_balance", type="number", format="money_usd", description="Current savings balance"))
    last = steps[-1].id
    return Artifact(
        capability_id="member_savings_balance_lookup",
        version="1.0.0",
        description="Look up a member by number and read the current savings balance.",
        app=AppIdentity(vendor="FictionalVendor", product="CORE-SERV Teller Platform", version="7.2", entry_url=base_url + "/"),
        inputs=[InputParam(name="member_id", type="string", pattern=r"^\d{5}$", description="Member number", example="10023")],
        outputs=outputs,
        steps=steps,
        checkpoints=[Checkpoint(id="cp01", after_step="s04", condition=text_present("Member Search"), description="signed on"),
                     Checkpoint(id="cp02", after_step=last, condition=text_present("Member Profile"), description="profile visible")],
        outcome_detectors=[
            OutcomeDetector(code="MEMBER_NOT_FOUND", description="No member matches the supplied number.",
                            condition=text_present("No member found"), after_steps=["s06"]),
            OutcomeDetector(code="PERMISSION_DENIED", description="Operator role lacks rights for this screen.",
                            condition=text_present("SEC-403")),
        ],
        recoveries=[
            Recovery(id="r01", description="Dismiss maintenance notice", trigger=text_present("SYSTEM NOTICE"),
                     action=RecoveryAction.DISMISS, locators=[loc_role("button", "Continue")], max_attempts=2),
            Recovery(id="r02", description="Re-authenticate after session expiry", trigger=text_present("SEC-440"),
                     action=RecoveryAction.REAUTH, goto_step="s01", max_attempts=1),
            Recovery(id="r03", description="Retry after transient server error", trigger=text_present("HTTP 500"),
                     action=RecoveryAction.RETRY, max_attempts=2, backoff_ms=200),
            Recovery(id="r04", description="Accept the unsaved-changes confirm dialog",
                     trigger=Condition(kind=ConditionKind.DIALOG_OPEN, pattern="Unsaved changes"),
                     action=RecoveryAction.ACCEPT_DIALOG, max_attempts=2),
        ],
        provenance=Provenance(recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc), model="fixture", run_id="fixture",
                              goal="look up member 10023 and read the savings balance", discovery_steps=7),
    )
