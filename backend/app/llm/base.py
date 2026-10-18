"""Provider-agnostic LLM interface. Everything else in the app depends on this, never on Groq directly."""
from dataclasses import dataclass, field
from typing import Any, Protocol

LLM_ERROR_KINDS = ("auth", "rate_limit", "timeout", "bad_request", "server", "unavailable")


class LLMError(Exception):
    """Any failure talking to the model. ``kind`` is one of LLM_ERROR_KINDS."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string exactly as the model sent it (it may be malformed)


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    model: str = ""


class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse: ...
