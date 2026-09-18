from pathlib import Path

import pytest

from cuc.policy import Policy
from cuc.schema import ActionType, RiskClass

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def policy():
    return Policy.load(ROOT / "policy.yaml")


def test_origin_and_route_allowlist(policy):
    assert policy.check_url("http://127.0.0.1:5055/members/10023").allowed
    assert policy.check_url("http://127.0.0.1:5055/members/10023/subaccount/confirm").allowed
    d = policy.check_url("http://evil.example/members/1")
    assert not d.allowed and "origin" in d.reason
    d = policy.check_url("http://127.0.0.1:5055/admin/fault/server_error")
    assert not d.allowed and "denied" in d.reason
    d = policy.check_url("http://127.0.0.1:5055/reports")
    assert not d.allowed and "not allowlisted" in d.reason


def test_action_allowlist(policy):
    assert policy.check_action(ActionType.CLICK, "http://127.0.0.1:5055/login").allowed
    p = Policy({"allow": {"origins": ["http://127.0.0.1:5055"], "routes": ["/**"], "actions": ["click"]}})
    assert not p.check_action(ActionType.TYPE, "http://127.0.0.1:5055/login").allowed


def test_risk_classification_takes_the_stricter_view(policy):
    assert policy.classify(ActionType.CLICK, "button", "Open Account") is RiskClass.IRREVERSIBLE
    assert policy.classify(ActionType.CLICK, "button", "Search") is RiskClass.REVERSIBLE
    assert policy.classify(ActionType.CLICK, "button", "Search", declared=RiskClass.IRREVERSIBLE) is RiskClass.IRREVERSIBLE
    assert policy.classify(ActionType.EXTRACT, None, None) is RiskClass.READ
    assert policy.classify(ActionType.CLICK, "button", "Open Account", declared=RiskClass.REVERSIBLE) is RiskClass.IRREVERSIBLE


def test_irreversible_gate_fails_closed(policy):
    g = policy.gate(RiskClass.IRREVERSIBLE)
    assert not g.allowed and g.requires_human
    assert policy.gate(RiskClass.IRREVERSIBLE, human_confirmed=True).allowed
    assert policy.gate(RiskClass.REVERSIBLE).allowed
    blocked = Policy({"risk": {"irreversible_mode": "block"}})
    g = blocked.gate(RiskClass.IRREVERSIBLE, human_confirmed=True)
    assert not g.allowed and not g.requires_human
    with pytest.raises(ValueError):
        Policy({"risk": {"irreversible_mode": "yolo"}})


def test_empty_policy_denies_everything():
    p = Policy({})
    assert not p.check_url("http://127.0.0.1:5055/").allowed
    assert not p.check_action(ActionType.CLICK, "http://127.0.0.1:5055/").allowed
