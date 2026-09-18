import json

from cuc.catalog import Catalog
from cuc.schema import RunResult, RunStatus
from fixtures import make_artifact


def test_catalog_lists_typed_tools_and_invokes_by_name(tmp_path):
    a = make_artifact()
    (tmp_path / "member_savings_balance_lookup.json").write_text(a.model_dump_json(indent=2))
    (tmp_path / "member_savings_balance_lookup-1.1.0.json").write_text(a.model_copy(update={"version": "1.1.0"}).model_dump_json())
    (tmp_path / "junk.json").write_text(json.dumps({"not": "an artifact"}))
    cat = Catalog(tmp_path)
    tools = cat.tools()
    assert [t.name for t in tools] == ["member_savings_balance_lookup"] and tools[0].version == "1.1.0"
    td = tools[0].as_tool_definition()
    assert td["input_schema"]["required"] == ["member_id"] and "MEMBER_NOT_FOUND" in td["description"]
    assert tools[0].output_schema["properties"]["savings_balance"]["format"] == "money_usd"

    calls = []

    def runner(artifact, args):
        calls.append((artifact.version, args))
        return RunResult(status=RunStatus.SUCCESS, run_id="r", capability_id=artifact.capability_id, version=artifact.version,
                         started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:00:01Z", outputs={"savings_balance": 1.0}, evidence_dir="x")

    r = cat.invoke("member_savings_balance_lookup", {"member_id": "10023"}, runner)
    assert r.status is RunStatus.SUCCESS and calls == [("1.1.0", {"member_id": "10023"})]
    try:
        cat.invoke("member_savings_balance_lookup", {"member_id": "x"}, runner)
        assert False, "invalid args must be rejected before the runner"
    except ValueError:
        pass
    assert len(calls) == 1
