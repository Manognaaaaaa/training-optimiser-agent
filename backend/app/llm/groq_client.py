"""Groq client (OpenAI-compatible chat completions with tool calling), written with httpx."""
import time
from typing import Any

import httpx

from app.config import settings
from app.llm.base import LLMError, LLMResponse, ToolCall, Usage

MAX_ATTEMPTS = 3  # first try plus two retries on 429 / 5xx
MAX_RETRY_WAIT_SECONDS = 20.0


class GroqClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: float = 60,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.api_key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._sleep = sleep

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=2048, response_format=None) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if response_format:
            body["response_format"] = response_format
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        started = time.monotonic()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._http.post(self.url, json=body, headers=headers)
            except httpx.TimeoutException as err:
                raise LLMError("timeout", f"The LLM did not answer in time ({err.__class__.__name__}).") from err
            except httpx.HTTPError as err:
                raise LLMError("server", f"Could not reach the LLM service: {err.__class__.__name__}.") from err

            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < MAX_ATTEMPTS:
                self._sleep(self._wait_seconds(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise self._error_for(resp)
            return self._parse(resp, int((time.monotonic() - started) * 1000))
        raise LLMError("server", "The LLM service kept failing.")  # not reachable; keeps type checkers happy

    @staticmethod
    def _wait_seconds(resp: httpx.Response, attempt: int) -> float:
        """Honour retry-after when present, otherwise back off 1s, 2s, ..."""
        header = resp.headers.get("retry-after")
        if header:
            try:
                return min(float(header), MAX_RETRY_WAIT_SECONDS)
            except ValueError:
                pass
        return float(2 ** (attempt - 1))

    def _error_for(self, resp: httpx.Response) -> LLMError:
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except (ValueError, AttributeError):
            pass
        code = resp.status_code
        if code in (401, 403):
            return LLMError("auth", "The LLM rejected the API key (check GROQ_API_KEY in backend/.env).")
        if code == 429:
            return LLMError("rate_limit", "The LLM rate limit was hit and retries did not help.")
        if code >= 500:
            return LLMError("server", f"The LLM service failed with HTTP {code}.")
        hint = f" Check that GROQ_MODEL ({self.model}) supports tool calling on Groq." if code in (400, 404) else ""
        return LLMError("bad_request", f"The LLM rejected the request (HTTP {code}): {detail[:300]}.{hint}")

    def _parse(self, resp: httpx.Response, latency_ms: int) -> LLMResponse:
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]
            calls = [
                ToolCall(id=c.get("id", ""), name=c["function"]["name"], arguments=c["function"].get("arguments") or "")
                for c in (message.get("tool_calls") or [])
            ]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as err:
            raise LLMError("server", "The LLM returned a response in an unexpected format.") from err
        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content"),
            tool_calls=calls,
            finish_reason=choice.get("finish_reason"),
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), usage.get("total_tokens", 0)),
            latency_ms=latency_ms,
            model=data.get("model", self.model),
        )


def build_groq_client() -> GroqClient:
    return GroqClient(settings.groq_api_key, settings.groq_model, settings.groq_base_url, settings.llm_timeout_seconds)
