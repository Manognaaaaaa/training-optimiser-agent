"""GroqClient against a mocked HTTP transport. No network."""
import json

import httpx
import pytest

from app.llm import get_llm_client
from app.llm.base import LLMError
from app.llm.groq_client import GroqClient

OK_BODY = {
    "model": "m1",
    "choices": [{"finish_reason": "tool_calls", "message": {
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "get_alert_context", "arguments": '{"alert_id": 1}'}}],
    }}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
}


def client_with(handler, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return GroqClient("secret-key", "m1", "https://example.test/v1", transport=httpx.MockTransport(handler), sleep=sleeps.append)


def test_request_shape_and_response_parsing():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    tools = [{"type": "function", "function": {"name": "get_alert_context", "parameters": {"type": "object"}}}]
    resp = client_with(handler).chat([{"role": "user", "content": "hi"}], tools=tools, temperature=0.3)
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["body"]["model"] == "m1" and seen["body"]["tools"] == tools and seen["body"]["temperature"] == 0.3
    assert resp.tool_calls[0].name == "get_alert_context" and resp.tool_calls[0].arguments == '{"alert_id": 1}'
    assert resp.usage.total_tokens == 10 and resp.finish_reason == "tool_calls" and resp.model == "m1"


def test_retries_on_429_then_succeeds_and_honours_retry_after():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=OK_BODY)

    resp = client_with(handler, sleeps).chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 2 and sleeps == [3.0] and resp.usage.total_tokens == 10


def test_gives_up_after_three_attempts():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={})

    with pytest.raises(LLMError) as err:
        client_with(handler).chat([{"role": "user", "content": "hi"}])
    assert err.value.kind == "server" and len(calls) == 3


def test_401_is_auth_error_and_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(LLMError) as err:
        client_with(handler).chat([{"role": "user", "content": "hi"}])
    assert err.value.kind == "auth" and len(calls) == 1
    assert "secret-key" not in str(err.value)


def test_timeout_maps_to_timeout_kind():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError) as err:
        client_with(handler).chat([{"role": "user", "content": "hi"}])
    assert err.value.kind == "timeout"


def test_bad_request_mentions_tool_calling_model():
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "tools not supported"}})

    with pytest.raises(LLMError) as err:
        client_with(handler).chat([{"role": "user", "content": "hi"}])
    assert err.value.kind == "bad_request" and "tool calling" in err.value.message


def test_get_llm_client_unavailable_without_key(monkeypatch):
    monkeypatch.setattr("app.llm.settings.groq_api_key", "")
    with pytest.raises(LLMError) as err:
        get_llm_client()
    assert err.value.kind == "unavailable" and "GROQ_API_KEY" in err.value.message
