import json

from cuc.cli import build_parser, _parse_params


def test_parser_covers_every_command():
    ap = build_parser()
    a = ap.parse_args(["discover", "--goal", "g", "--target", "http://x/", "--secret", "A=desc", "--escalate"])
    assert a.fn.__name__ == "cmd_discover" and a.secret == ["A=desc"] and a.escalate and not a.headless
    a = ap.parse_args(["replay", "--artifact", "a.json", "--param", "member_id=10023", "--headless"])
    assert a.fn.__name__ == "cmd_replay" and a.headless
    a = ap.parse_args(["resume", "--run-id", "r1", "--decision", "confirmed"])
    assert a.fn.__name__ == "cmd_resume"
    assert ap.parse_args(["catalog", "list", "--json"]).as_json
    assert ap.parse_args(["catalog", "invoke", "cap", "--params", '{"member_id": "1"}']).name == "cap"
    assert ap.parse_args(["schema", "export"]).fn.__name__ == "cmd_schema"
    assert ap.parse_args(["target-app", "serve", "--port", "5056"]).port == 5056


def test_param_parsing_merges_json_and_pairs():
    assert _parse_params(["member_id=10023", "pin=secret:MEMBER_PIN"], json.dumps({"x": 1})) == {"x": 1, "member_id": "10023", "pin": "secret:MEMBER_PIN"}
