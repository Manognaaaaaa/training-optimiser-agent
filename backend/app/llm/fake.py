"""A scripted stand-in for the LLM. Every test uses this, so no test needs a network."""
import json
from typing import Any

from app.llm.base import LLMResponse, ToolCall, Usage


def tool_response(name: str, arguments: dict[str, Any] | str, call_id: str = "c1", tokens: int = 10) -> LLMResponse:
    """A response that calls one tool. ``arguments`` may be a raw (even malformed) string."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return LLMResponse(
        tool_calls=[ToolCall(call_id, name, raw)], finish_reason="tool_calls",
        usage=Usage(tokens // 2, tokens - tokens // 2, tokens), latency_ms=5, model="fake",
    )


def text_response(text: str, tokens: int = 10) -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop", usage=Usage(tokens // 2, tokens - tokens // 2, tokens), latency_ms=5, model="fake")


class FakeLLMClient:
    """Returns the scripted items in order. An item that is an exception (e.g. LLMError) is raised instead."""

    model = "fake"

    def __init__(self, script: list[LLMResponse | Exception]):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=2048, response_format=None) -> LLMResponse:
        # Copy the messages: the caller keeps appending to its own list.
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools, "temperature": temperature})
        if not self.script:
            raise AssertionError("FakeLLMClient script ran out")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
