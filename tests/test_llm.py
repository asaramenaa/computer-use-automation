"""Provider adapters without network: backoff semantics and the Groq (OpenAI-compatible) message mapping."""
import json
from types import SimpleNamespace

import pytest

from cuc.agent import llm as L
from cuc.agent.llm import GroqClient, LLMError, ModelTurn, RateLimited, ToolResult, ToolSpec, with_backoff


def test_with_backoff_honours_retry_after_then_gives_up(monkeypatch):
    waits = []
    monkeypatch.setattr(L.time, "sleep", lambda s: waits.append(s))
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RateLimited("429", retry_after=7)
        return ModelTurn("ok", [])

    assert with_backoff(flaky, max_attempts=3, base_delay=10).text == "ok" and waits == [7, 7]
    with pytest.raises(LLMError, match="giving up"):
        with_backoff(lambda: (_ for _ in ()).throw(RateLimited("429")), max_attempts=2, base_delay=1)
    assert 1 <= waits[-1] <= 1.25  # exponential with jitter when no retry-after


class FakeCompletions:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})  # snapshot: the client mutates its list afterwards
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _reply(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def _tc(id_, name, args):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_groq_maps_tools_messages_and_tool_results():
    comp = FakeCompletions([_reply(tool_calls=[_tc("call_1", "click", {"frame": "main", "role": "button", "name": "Search", "reason": "r"})]),
                            _reply(content="done thinking", tool_calls=None)])
    client = GroqClient("openai/gpt-oss-120b", client=SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    client.start("SYSTEM", [ToolSpec("click", "Click", {"type": "object", "properties": {}})])
    turn = client.send_user("go")
    assert turn.tool_calls[0].name == "click" and turn.tool_calls[0].args["name"] == "Search" and turn.usage == {"input_tokens": 10, "output_tokens": 5}
    req = comp.requests[0]
    assert req["model"] == "openai/gpt-oss-120b" and req["tools"][0]["function"]["name"] == "click" and req["tool_choice"] == "auto"
    assert req["messages"][0] == {"role": "system", "content": "SYSTEM"}
    turn = client.send_tool_results([ToolResult("call_1", "click", "boom", is_error=True)])
    msgs = comp.requests[1]["messages"]
    assert msgs[-2]["role"] == "assistant" and msgs[-2]["tool_calls"][0]["id"] == "call_1"
    assert msgs[-1] == {"role": "tool", "tool_call_id": "call_1", "name": "click", "content": "ERROR: boom"}
    assert turn.text == "done thinking" and turn.tool_calls == []


def test_groq_429_uses_retry_after_header(monkeypatch):
    import openai

    waits = []
    monkeypatch.setattr(L.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    err = openai.RateLimitError("rate limited", response=SimpleNamespace(status_code=429, headers={"retry-after": "3"}, request=None), body=None)
    comp = FakeCompletions([err, _reply(content="ok")])
    client = GroqClient("m", client=SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    client.start("s", [])
    assert client.send_user("x").text == "ok" and waits == [4.0]


def test_factory_defaults_to_groq(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")
    c = L.make_client()
    assert c.provider == "groq" and c.model == "openai/gpt-oss-120b"
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(LLMError):
        L.make_client()


def test_history_is_compacted_to_the_latest_observation():
    from cuc.agent.llm import elide

    obs = "click ok\n\nObservation:\nPage title: x\n### Frame: main\n...lots..."
    assert elide(obs).startswith("click ok\n[observation elided")
    assert elide("plain error text") == "plain error text"
    comp = FakeCompletions([_reply(tool_calls=[_tc("c1", "click", {})]), _reply(tool_calls=[_tc("c2", "click", {})]), _reply(content="end")])
    client = GroqClient("m", client=SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    client.start("s", [])
    client.send_user("GOAL: g\n\nCurrent observation:\nbig tree")
    client.send_tool_results([ToolResult("c1", "click", obs)])
    client.send_tool_results([ToolResult("c2", "click", obs)])
    msgs = comp.requests[2]["messages"]
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert "elided" in tool_msgs[0]["content"] and "lots" not in tool_msgs[0]["content"]
    assert "lots" in tool_msgs[1]["content"]  # only the latest observation is sent in full
    assert msgs[1]["content"].startswith("GOAL: g\n[observation elided")


def test_connection_errors_and_5xx_are_retried_not_fatal(monkeypatch):
    import openai

    waits = []
    monkeypatch.setattr(L.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setenv("LLM_MAX_RETRIES", "3")
    monkeypatch.setenv("LLM_RETRY_BASE_SECONDS", "1")
    conn = openai.APIConnectionError(request=None)
    server = openai.InternalServerError("boom", response=SimpleNamespace(status_code=503, headers={}, request=None), body=None)
    comp = FakeCompletions([conn, server, _reply(content="ok")])
    client = GroqClient("m", client=SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    client.start("s", [])
    assert client.send_user("x").text == "ok" and len(waits) == 2
    bad = openai.BadRequestError("nope", response=SimpleNamespace(status_code=400, headers={}, request=None), body=None)
    client2 = GroqClient("m", client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([bad]))))
    client2.start("s", [])
    with pytest.raises(LLMError, match="groq 400"):
        client2.send_user("x")


def test_groq_tool_argument_validation_failure_is_fed_back_and_retried(monkeypatch):
    import openai

    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    body = {"error": {"code": "tool_use_failed", "message": "parameters for tool type_text did not match schema: /name expected string, got null"}}
    bad = openai.BadRequestError("Tool call validation failed", response=SimpleNamespace(status_code=400, headers={}, request=None), body=body)
    comp = FakeCompletions([bad, _reply(tool_calls=[_tc("c1", "type_text", {"frame": "main", "role": "textbox", "anchor": "User ID", "text": "x", "reason": "r"})])])
    client = GroqClient("m", client=SimpleNamespace(chat=SimpleNamespace(completions=comp)))
    client.start("s", [])
    turn = client.send_user("go")
    assert turn.tool_calls[0].name == "type_text" and turn.attempts == 2
    assert comp.requests[1]["messages"][-1]["role"] == "user" and "rejected by argument validation" in comp.requests[1]["messages"][-1]["content"]


def test_tool_schemas_allow_null_for_optional_fields():
    from cuc.agent.tools import TOOLS

    t = next(x for x in TOOLS if x.name == "type_text")
    assert t.input_schema["properties"]["name"]["type"] == ["string", "null"]
    assert t.input_schema["properties"]["param_name"]["type"] == ["string", "null"]
    assert "name" not in t.input_schema["required"]
