"""Provider-agnostic LLM client for the discovery loop.

The loop only sees ``ToolSpec`` in and ``ModelTurn`` out. Each provider keeps its own
conversation state in its native message format. Selected by ``LLM_PROVIDER`` (groq |
gemini | anthropic) and ``LLM_MODEL`` from the environment. Rate limits (HTTP 429) are retried with
exponential backoff and a hard attempt cap so a free-tier quota is not burned.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0   # wall time of the successful request only
    attempts: int = 1        # requests made including retried ones


class LLMError(Exception):
    """Non-retryable provider failure (auth, bad request, refusal, exhausted retries)."""


class RateLimited(Exception):
    """Retryable: HTTP 429."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Transient(RateLimited):
    """Retryable: connection error, timeout, or 5xx. Shares the bounded backoff with 429."""


REQUEST_TIMEOUT_S = float(os.environ.get("LLM_REQUEST_TIMEOUT_S", "120"))  # a hung request fails fast and is retried


class LLMClient(Protocol):
    provider: str
    model: str

    def start(self, system: str, tools: list[ToolSpec]) -> None: ...
    def send_user(self, text: str) -> ModelTurn: ...
    def send_tool_results(self, results: list[ToolResult]) -> ModelTurn: ...


# ------------------------------------------------------------------- history size
_ELIDED = "\n[observation elided: superseded by a later one]"


def elide(text: str) -> str:
    """Keep only the first line of a message that carries an observation. Every request re-sends the whole
    conversation, so only the latest observation is worth its tokens; earlier ones are stale by definition."""
    if "Observation:" in text or "Current observation:" in text:
        return text.split("\n", 1)[0] + _ELIDED
    return text


# --------------------------------------------------------------------------- retry
def with_backoff(fn: Callable[[], ModelTurn], max_attempts: int, base_delay: float,
                 on_wait: Callable[[int, float, str], None] | None = None) -> ModelTurn:
    """Call ``fn``; on RateLimited/Transient sleep (retry_after or exponential backoff with jitter) and retry."""
    for attempt in range(1, max_attempts + 1):
        started = time.monotonic()
        try:
            turn = fn()
            turn.elapsed_s, turn.attempts = round(time.monotonic() - started, 2), attempt
            return turn
        except RateLimited as e:
            if attempt >= max_attempts:
                raise LLMError(f"retryable failure {attempt} times; giving up to protect quota: {e}") from e
            delay = e.retry_after if e.retry_after else base_delay * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
            if on_wait:
                on_wait(attempt, delay, str(e))
            time.sleep(min(delay, 120))
    raise LLMError("unreachable")


# -------------------------------------------------------------------------- gemini
class GeminiClient:
    provider = "gemini"

    def __init__(self, model: str, api_key: str | None = None, temperature: float = 0.0):
        from google import genai
        from google.genai import types

        self._types = types
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set")
        # SDK retries are disabled: the loop owns backoff so attempts are capped and visible in the log.
        self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_S * 1000),
                                                                            retry_options=types.HttpRetryOptions(attempts=1)))
        self.model = model
        self._temperature = temperature
        self._contents: list[Any] = []
        self._config: Any = None

    def start(self, system: str, tools: list[ToolSpec]) -> None:
        t = self._types
        decls = [t.FunctionDeclaration(name=s.name, description=s.description, parameters_json_schema=s.input_schema) for s in tools]
        self._config = t.GenerateContentConfig(
            system_instruction=system,
            tools=[t.Tool(function_declarations=decls)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            temperature=self._temperature,
        )
        self._contents = []

    def send_user(self, text: str) -> ModelTurn:
        t = self._types
        self._contents.append(t.Content(role="user", parts=[t.Part.from_text(text=text)]))
        return self._generate()

    def send_tool_results(self, results: list[ToolResult]) -> ModelTurn:
        t = self._types
        self._compact()
        parts = [t.Part.from_function_response(name=r.name, response={"error" if r.is_error else "result": r.content}) for r in results]
        self._contents.append(t.Content(role="tool", parts=parts))
        return self._generate()

    def _compact(self) -> None:
        t = self._types
        for i, c in enumerate(self._contents):
            if c.role == "tool":
                new_parts = []
                for part in c.parts or []:
                    fr = part.function_response
                    if fr is not None:
                        resp = {k: (elide(v) if isinstance(v, str) else v) for k, v in (fr.response or {}).items()}
                        new_parts.append(t.Part.from_function_response(name=fr.name, response=resp))
                    else:
                        new_parts.append(part)
                self._contents[i] = t.Content(role="tool", parts=new_parts)
            elif c.role == "user":
                self._contents[i] = t.Content(role="user", parts=[t.Part.from_text(text=elide(pp.text)) if pp.text else pp for pp in (c.parts or [])])

    def _generate(self) -> ModelTurn:
        from google.genai import errors

        def call() -> ModelTurn:
            try:
                resp = self._client.models.generate_content(model=self.model, contents=self._contents, config=self._config)
            except errors.APIError as e:
                if e.code == 429:
                    raise RateLimited(f"gemini 429: {e.message}", _retry_after_from(e)) from e
                if e.code and e.code >= 500:
                    raise Transient(f"gemini {e.code}: {e.message}") from e
                raise LLMError(f"gemini {e.code}: {e.message}") from e
            except (ConnectionError, TimeoutError, OSError) as e:
                raise Transient(f"gemini connection error: {e}") from e
            if not resp.candidates:
                raise LLMError(f"gemini returned no candidates (prompt_feedback={resp.prompt_feedback})")
            content = resp.candidates[0].content
            self._contents.append(content)
            text_parts, calls = [], []
            for i, part in enumerate(content.parts or []):
                if part.function_call is not None:
                    fc = part.function_call
                    calls.append(ToolCall(id=fc.id or f"call_{len(self._contents)}_{i}", name=fc.name or "", args=dict(fc.args or {})))
                elif part.text:
                    text_parts.append(part.text)
            usage = {}
            if resp.usage_metadata:
                usage = {"input_tokens": resp.usage_metadata.prompt_token_count or 0, "output_tokens": resp.usage_metadata.candidates_token_count or 0}
            return ModelTurn(text="\n".join(text_parts), tool_calls=calls, usage=usage)

        return with_backoff(call, _max_attempts(), _base_delay(), _log_wait)


def _retry_after_from(e: Any) -> float | None:
    # Gemini 429 bodies carry google.rpc.RetryInfo {"retryDelay": "12s"}; use it when present.
    try:
        for d in (e.details or {}).get("error", {}).get("details", []):
            if "retryDelay" in d:
                return float(str(d["retryDelay"]).rstrip("s")) + 1
    except Exception:
        pass
    return None


# ------------------------------------------------------------------------ anthropic
class AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str, temperature: float = 0.0):
        import anthropic

        self._sdk = anthropic
        # Client resolves ANTHROPIC_API_KEY / auth profile itself. SDK retries off: the loop owns backoff.
        self._client = anthropic.Anthropic(max_retries=0, timeout=REQUEST_TIMEOUT_S)
        self.model = model
        self._messages: list[dict[str, Any]] = []
        self._system = ""
        self._tools: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[ToolSpec]) -> None:
        self._system = system
        self._tools = [{"name": s.name, "description": s.description, "input_schema": s.input_schema} for s in tools]
        self._messages = []

    def send_user(self, text: str) -> ModelTurn:
        self._messages.append({"role": "user", "content": text})
        return self._generate()

    def send_tool_results(self, results: list[ToolResult]) -> ModelTurn:
        self._compact()
        self._messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error} for r in results]})
        return self._generate()

    def _compact(self) -> None:
        for m in self._messages:
            if m["role"] != "user":
                continue
            if isinstance(m["content"], str):
                m["content"] = elide(m["content"])
            else:
                for block in m["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                        block["content"] = elide(block["content"])

    def _generate(self) -> ModelTurn:
        sdk = self._sdk

        def call() -> ModelTurn:
            try:
                resp = self._client.messages.create(model=self.model, max_tokens=4096, system=self._system,
                                                    tools=self._tools, messages=self._messages)
            except sdk.RateLimitError as e:
                raise RateLimited(f"anthropic 429: {e.message}") from e
            except sdk.APIStatusError as e:
                if e.status_code >= 500:
                    raise Transient(f"anthropic {e.status_code}: {e.message}") from e
                raise LLMError(f"anthropic {e.status_code}: {e.message}") from e
            except sdk.APIConnectionError as e:  # includes timeouts
                raise Transient(f"anthropic connection error: {e}") from e
            if resp.stop_reason == "refusal":
                raise LLMError("anthropic refused the request (stop_reason=refusal)")
            self._messages.append({"role": "assistant", "content": resp.content})
            text = "".join(b.text for b in resp.content if b.type == "text")
            calls = [ToolCall(id=b.id, name=b.name, args=dict(b.input)) for b in resp.content if b.type == "tool_use"]
            return ModelTurn(text=text, tool_calls=calls, usage={"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens})

        return with_backoff(call, _max_attempts(), _base_delay(), _log_wait)


# ----------------------------------------------------------------------- groq
class GroqClient:
    """Groq via its OpenAI-compatible endpoint. Function calling through chat.completions tools."""

    provider = "groq"
    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, model: str, api_key: str | None = None, temperature: float = 0.0, client: Any = None):
        import openai

        self._sdk = openai
        key = api_key or os.environ.get("GROQ_API_KEY")
        if client is None and not key:
            raise LLMError("GROQ_API_KEY is not set")
        # SDK retries off: the loop owns backoff so attempts are capped and visible in the log.
        self._client = client or openai.OpenAI(base_url=self.BASE_URL, api_key=key, max_retries=0, timeout=REQUEST_TIMEOUT_S)
        self.model = model
        self._temperature = temperature
        self._messages: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        # Reasoning models on Groq (gpt-oss) accept reasoning_effort; low keeps per-turn output small on a free tier.
        self._extra: dict[str, Any] = {}
        effort = os.environ.get("LLM_REASONING_EFFORT", "low")
        if effort:
            self._extra["reasoning_effort"] = effort

    def start(self, system: str, tools: list[ToolSpec]) -> None:
        self._tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}} for t in tools]
        self._messages = [{"role": "system", "content": system}]

    def send_user(self, text: str) -> ModelTurn:
        self._messages.append({"role": "user", "content": text})
        return self._generate()

    def send_tool_results(self, results: list[ToolResult]) -> ModelTurn:
        for m in self._messages:
            if m["role"] in ("tool", "user") and isinstance(m.get("content"), str):
                m["content"] = elide(m["content"])
        for r in results:
            self._messages.append({"role": "tool", "tool_call_id": r.call_id, "name": r.name,
                                   "content": ("ERROR: " if r.is_error else "") + r.content})
        return self._generate()

    def _generate(self) -> ModelTurn:
        sdk = self._sdk
        corrections = {"n": 0}

        def call() -> ModelTurn:
            try:
                resp = self._client.chat.completions.create(model=self.model, messages=self._messages, tools=self._tools,
                                                            tool_choice="auto", temperature=self._temperature, **self._extra)
            except sdk.RateLimitError as e:
                raise RateLimited(f"groq 429: {getattr(e, 'message', e)}", _retry_after_header(e)) from e
            except sdk.APIStatusError as e:
                if e.status_code >= 500:
                    raise Transient(f"groq {e.status_code}: {getattr(e, 'message', e)}") from e
                if e.status_code == 400 and _error_code(e) == "tool_use_failed" and corrections["n"] < 2:
                    # Groq validates tool arguments server-side and rejects the whole turn. Feed the validation
                    # message back as a user message and retry, at most twice per turn.
                    corrections["n"] += 1
                    self._messages.append({"role": "user", "content": "Your previous tool call was rejected by argument validation: "
                                           f"{getattr(e, 'message', e)}. Re-issue the call with valid arguments (omit optional fields you do not need)."})
                    raise Transient(f"groq tool_use_failed (correction {corrections['n']})", retry_after=0.5) from e
                raise LLMError(f"groq {e.status_code}: {getattr(e, 'message', e)}") from e
            except sdk.APIConnectionError as e:  # includes timeouts
                raise Transient(f"groq connection error: {e}") from e
            if not resp.choices:
                raise LLMError("groq returned no choices")
            msg = resp.choices[0].message
            assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            calls: list[ToolCall] = []
            if msg.tool_calls:
                assistant["tool_calls"] = [{"id": tc.id, "type": "function",
                                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in msg.tool_calls]
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {"_malformed_arguments": tc.function.arguments}
                    calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args if isinstance(args, dict) else {"value": args}))
            self._messages.append(assistant)
            usage = {}
            if resp.usage:
                usage = {"input_tokens": resp.usage.prompt_tokens or 0, "output_tokens": resp.usage.completion_tokens or 0}
            return ModelTurn(text=msg.content or "", tool_calls=calls, usage=usage)

        return with_backoff(call, _max_attempts(), _base_delay(), _log_wait)


def _error_code(e: Any) -> str | None:
    try:
        return (e.body or {}).get("error", {}).get("code") or (e.body or {}).get("code")
    except Exception:
        return None


def _retry_after_header(e: Any) -> float | None:
    """Groq sends Retry-After (seconds) on 429; honour it, plus a small margin."""
    try:
        v = e.response.headers.get("retry-after")
        return float(v) + 1 if v else None
    except Exception:
        return None


# -------------------------------------------------------------------------- factory
def _max_attempts() -> int:
    return int(os.environ.get("LLM_MAX_RETRIES", "4")) + 1


def _base_delay() -> float:
    return float(os.environ.get("LLM_RETRY_BASE_SECONDS", "10"))


def _log_wait(attempt: int, delay: float, msg: str) -> None:
    print(f"[llm {time.strftime('%H:%M:%S')}] retryable failure (attempt {attempt}); waiting {delay:.0f}s: {msg[:120]}", flush=True)


def make_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    provider = (provider or os.environ.get("LLM_PROVIDER", "groq")).lower()
    model = model or os.environ.get("LLM_MODEL")
    if not model:
        raise LLMError("LLM_MODEL is not set")
    if provider == "groq":
        return GroqClient(model)
    if provider == "gemini":
        return GeminiClient(model)
    if provider == "anthropic":
        return AnthropicClient(model)
    raise LLMError(f"unknown LLM_PROVIDER {provider!r} (groq | gemini | anthropic)")
