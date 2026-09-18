import pytest

from cuc.schema import ActionType, Condition, ConditionKind, ExtractSpec, ExtractStrategy, Locator, LocatorStrategy
from cuc.surface import LocatorError, SurfaceError
from cuc.surface.playwright_surface import parse_value
from conftest import login_via_surface
from fixtures import loc_anchor, loc_role, text_present


def test_observe_frames_and_unnamed_controls(surface, live_server):
    surface.act(ActionType.NAVIGATE, None, live_server + "/", 10_000)
    assert surface.wait_for(text_present("Sign On"), 5000)
    obs = surface.observe()
    paths = {f.path for f in obs.frames}
    assert {"nav", "main"} <= paths
    main = next(f for f in obs.frames if f.path == "main")
    assert "textbox" in main.a11y
    boxes = [c for c in main.controls if c.role == "textbox"]
    assert [c.anchor for c in boxes] == ["User ID", "Password"] and all(c.name == "" for c in boxes)
    assert any(c.role == "button" and c.name == "Sign On" for c in main.controls)
    prompt = obs.to_prompt()
    assert 'anchor="User ID"' in prompt and "### Frame: main" in prompt
    assert obs.state_hash() == surface.observe().state_hash()


def test_resolution_order_and_attempt_reporting(surface, live_server):
    surface.act(ActionType.NAVIGATE, None, live_server + "/", 10_000)
    surface.wait_for(text_present("Sign On"), 5000)
    locs = [loc_role("textbox", "User ID"), loc_anchor("textbox", "User ID")]  # role_name cannot work: input has no name
    r = surface.resolve(locs, 2000)
    assert r.strategy is LocatorStrategy.ANCHOR
    with pytest.raises(LocatorError) as ei:
        surface.resolve([loc_role("button", "Nope"), loc_anchor("textbox", "Nothing here")], 500)
    kinds = [a["strategy"] for a in ei.value.attempts]
    assert kinds == ["role_name", "anchor"] and all(a["matches"] == 0 for a in ei.value.attempts)


def test_ambiguity_is_failure_not_guess(surface, live_server):
    login_via_surface(surface, live_server)
    surface.act(ActionType.TYPE, surface.resolve([loc_anchor("textbox", "Member Number")], 5000), "10023", 5000)
    surface.act(ActionType.CLICK, surface.resolve([loc_role("button", "Search")], 5000), None, 5000)
    assert surface.wait_for(text_present("Member Profile"), 5000)
    ambiguous = Locator(strategy=LocatorStrategy.ROLE_NAME, frame="main", role="cell", name="S-0", exact=False, rationale="t")
    with pytest.raises(LocatorError) as ei:
        surface.resolve([ambiguous], 500)
    assert ei.value.attempts[0]["matches"] > 1
    # ambiguity on the first strategy falls through to a later unambiguous one
    r = surface.resolve([ambiguous, loc_role("link", "Open Sub-Account")], 2000)
    assert r.strategy is LocatorStrategy.ROLE_NAME and "Open Sub-Account" in r.description


def test_extract_table_cell_and_label_value(surface, live_server):
    login_via_surface(surface, live_server)
    surface.act(ActionType.NAVIGATE, None, live_server + "/members/10023", 10_000)
    raw = surface.extract(ExtractSpec(output="b", strategy=ExtractStrategy.TABLE_CELL, row_anchor="Savings", column_header="Balance"), 3000)
    assert raw == "$1,250.75" and parse_value(raw, "money") == 1250.75
    assert surface.extract(ExtractSpec(output="s", strategy=ExtractStrategy.LABEL_VALUE, label="Status"), 3000) == "Active"
    assert surface.extract(ExtractSpec(output="n", strategy=ExtractStrategy.URL_REGEX, pattern=r"/members/(\d+)"), 3000) == "10023"
    with pytest.raises(SurfaceError, match="exactly one"):
        surface.extract(ExtractSpec(output="x", strategy=ExtractStrategy.TABLE_CELL, row_anchor="S-0", column_header="Balance"), 300)


def tp(text):
    """Top-document condition: deep links below replace the frameset, so no frame path."""
    return text_present(text, frame=None)


def test_conditions_and_wait(surface, live_server):
    login_via_surface(surface, live_server)
    surface.act(ActionType.NAVIGATE, None, live_server + "/members/10023", 10_000)
    assert surface.wait_for(tp("Member Profile"), 5000)
    assert surface.check(Condition(kind=ConditionKind.URL_MATCHES, pattern=r"/members/\d+$"))
    assert surface.check(Condition(kind=ConditionKind.ELEMENT_PRESENT, locator=loc_role("link", "Open Sub-Account", frame=None)))
    assert surface.check(Condition(kind=ConditionKind.ELEMENT_ABSENT, locator=loc_role("button", "Sign On", frame=None)))
    assert surface.check(Condition(kind=ConditionKind.TEXT_ABSENT, text="SEC-440"))
    assert surface.check(text_present("Member Profile")) is False  # frame 'main' no longer exists: honest miss
    assert surface.wait_for(tp("never appears"), 300) is False


def test_slow_load_is_waited_for_not_slept(surface, live_server):
    login_via_surface(surface, live_server)
    surface.act(ActionType.NAVIGATE, None, live_server + "/members/10023?fault=slow", 10_000)
    assert surface.wait_for(tp("Member Profile"), 5000)


def test_unknown_dialog_is_dismissed_known_is_accepted(surface, live_server):
    login_via_surface(surface, live_server)
    surface.set_dialog_policy([])
    surface.drain_dialogs()
    surface.act(ActionType.NAVIGATE, None, live_server + "/members/search?fault=confirm_dialog", 10_000)
    assert surface.wait_for(tp("USR-CANCEL"), 5000)
    events = surface.drain_dialogs()
    assert events and events[0].handled == "dismissed" and events[0].kind == "confirm"
    surface.set_dialog_policy(["Unsaved changes"])
    surface.act(ActionType.NAVIGATE, None, live_server + "/members/search?fault=confirm_dialog", 10_000)
    assert surface.wait_for(tp("Member Number"), 5000)
    assert surface.check(Condition(kind=ConditionKind.DIALOG_OPEN, pattern="Unsaved changes"))
    assert surface.drain_dialogs()[0].handled == "accepted"
    surface.set_dialog_policy([])


def test_interstitial_blocks_until_dismissed(surface, live_server):
    login_via_surface(surface, live_server)
    surface.act(ActionType.NAVIGATE, None, live_server + "/members/search?fault=interstitial", 10_000)
    assert surface.wait_for(tp("SYSTEM NOTICE"), 5000)
    with pytest.raises(SurfaceError):  # overlay intercepts the click: actionability check fails
        surface.act(ActionType.CLICK, surface.resolve([loc_role("button", "Search", frame=None)], 2000), None, 800)
    surface.act(ActionType.CLICK, surface.resolve([loc_role("button", "Continue", frame=None)], 2000), None, 3000)
    assert surface.wait_for(Condition(kind=ConditionKind.TEXT_ABSENT, text="SYSTEM NOTICE"), 3000)
    surface.act(ActionType.CLICK, surface.resolve([loc_role("button", "Search", frame=None)], 2000), None, 3000)
    assert surface.wait_for(tp("No member found"), 5000)


def test_coordinates_last_resort_requires_matching_viewport(surface, live_server):
    login_via_surface(surface, live_server)
    obs = surface.observe()
    btn = next(c for f in obs.frames if f.path == "main" for c in f.controls if c.name == "Search")
    x, y = btn.bbox[0] + btn.bbox[2] / 2, btn.bbox[1] + btn.bbox[3] / 2
    stale = Locator(strategy=LocatorStrategy.COORDINATES, frame="main", x=x, y=y, viewport=(800, 600), rationale="t")
    with pytest.raises(LocatorError, match="viewport differs"):
        surface.resolve([stale], 300)
    ok = Locator(strategy=LocatorStrategy.COORDINATES, frame="main", x=x, y=y, viewport=(1280, 800), rationale="t")
    surface.act(ActionType.CLICK, surface.resolve([ok], 1000), None, 2000)
    assert surface.wait_for(text_present("No member found"), 5000)


def test_evidence_files(surface, live_server, tmp_path):
    login_via_surface(surface, live_server)
    assert surface.screenshot(tmp_path / "s.png").stat().st_size > 1000
    snap = surface.snapshot(tmp_path / "a11y.txt").read_text()
    assert "=== frame: main" in snap and "textbox" in snap
