"""OpenAI-compatible chat client with the fallbacks of design 14.3.

Fallbacks: drop ``temperature`` when the endpoint rejects it, use ``max_completion_tokens`` when it
rejects ``max_tokens``, switch to schema-constrained JSON replies when it rejects ``tools``, and
retry rate limits and server errors with waits of 1, 2, 4, and 8 seconds.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import openai

from simagent.config import LlmConfig
from simagent.events import Redactor

if TYPE_CHECKING:  # openai 3.x uses httpx2, openai 2.x uses httpx
    import httpx2 as httpx

BACKOFF_SECONDS = (1, 2, 4, 8)


class LlmUnavailable(Exception):
    """The endpoint cannot be used right now (the message never contains the API key)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    total_tokens: int = 0
    usage_estimated: bool = False
    used_json_fallback: bool = False


def _estimate_tokens(messages: Sequence[dict[str, Any]], reply: str | None) -> int:
    chars = sum(len(str(m.get("content") or "")) for m in messages) + len(reply or "")
    return -(-chars // 4)


def _json_system_message(functions: Sequence[dict[str, Any]]) -> dict[str, str]:
    described = [
        {"name": f["function"]["name"], "description": f["function"].get("description", ""),
         "parameters": f["function"].get("parameters", {})}
        for f in functions
    ]
    text = (
        "You can call these functions. Reply ONLY with one JSON object of the form "
        '{"tool": <function name or null>, "args": {...}, "message": "<text for the user>"}.\n'
        "Functions:\n" + json.dumps(described))
    return {"role": "system", "content": text}


def _parse_json_reply(content: str | None) -> tuple[list[ToolCall], str | None]:
    if not content:
        return [], content
    start = content.find("{")
    if start < 0:
        return [], content
    try:
        obj, _ = json.JSONDecoder().raw_decode(content[start:])
    except json.JSONDecodeError:
        return [], content
    if not isinstance(obj, dict):
        return [], content
    message = obj.get("message")
    tool = obj.get("tool")
    calls: list[ToolCall] = []
    if tool:
        args = obj.get("args")
        calls.append(ToolCall(id="json-1", name=str(tool),
                              arguments=args if isinstance(args, dict) else {}))
    return calls, message if isinstance(message, str) else content


class LlmClient:
    def __init__(self, cfg: LlmConfig, *, http_client: "httpx.Client | None" = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self._sleep = sleep
        self._redactor = Redactor([cfg.api_key or ""])
        self._omit_temperature = False
        self._max_completion_tokens = False
        self._json_mode = False
        self._no_response_format = False
        self._client = openai.OpenAI(
            base_url=cfg.base_url, api_key=cfg.api_key or "unset", http_client=http_client,
            max_retries=0, timeout=cfg.request_timeout_s)

    def _fail(self, message: str) -> LlmUnavailable:
        return LlmUnavailable(self._redactor.redact(message))

    def chat(self, messages: list[dict[str, Any]],
             functions: Sequence[dict[str, Any]] | None = None,
             max_tokens: int = 1024) -> ChatReply:
        if not self.cfg.api_key:
            raise self._fail("LUNA_API_KEY is not set")
        if not self.cfg.base_url:  # never fall back to the SDK's default (a third-party host)
            raise self._fail("LUNA_BASE_URL is not set")
        waits = list(BACKOFF_SECONDS)
        while True:
            use_json = self._json_mode and bool(functions)
            sent = [_json_system_message(functions), *messages] if use_json and functions \
                else list(messages)
            kwargs: dict[str, Any] = {"model": self.cfg.model, "messages": sent}
            if not self._omit_temperature:
                kwargs["temperature"] = self.cfg.temperature
            kwargs["max_completion_tokens" if self._max_completion_tokens else "max_tokens"] = \
                max_tokens
            if functions and not use_json:
                kwargs["tools"] = list(functions)
                kwargs["tool_choice"] = "auto"
            if use_json and not self._no_response_format:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = self._client.chat.completions.create(**kwargs)
            except openai.BadRequestError as exc:
                if self._adapt(str(exc).lower(), functions):
                    continue
                raise self._fail(f"bad request to the LLM endpoint: {exc}") from exc
            except (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError,
                    openai.APITimeoutError) as exc:
                if not waits:
                    raise self._fail(f"LLM endpoint unavailable after retries: {exc}") from exc
                self._sleep(waits.pop(0))
                continue
            except openai.AuthenticationError as exc:
                raise self._fail("LLM authentication failed; check LUNA_API_KEY") from exc
            except openai.APIStatusError as exc:
                raise self._fail(f"LLM endpoint error {exc.status_code}: {exc}") from exc
            return self._to_reply(response, sent, use_json)

    def _adapt(self, text: str, functions: Sequence[dict[str, Any]] | None) -> bool:
        """Remember an unsupported option so the retry (and later calls) avoid it."""
        if "temperature" in text and not self._omit_temperature:
            self._omit_temperature = True
            return True
        if "max_tokens" in text and not self._max_completion_tokens:
            self._max_completion_tokens = True
            return True
        if "response_format" in text and self._json_mode and not self._no_response_format:
            self._no_response_format = True
            return True
        if "tool" in text and functions and not self._json_mode:
            self._json_mode = True
            return True
        return False

    def _to_reply(self, response: Any, sent: list[dict[str, Any]], used_json: bool) -> ChatReply:
        message = response.choices[0].message
        content = message.content
        calls: list[ToolCall] = []
        if used_json:
            calls, content = _parse_json_reply(content)
        else:
            for item in message.tool_calls or []:
                try:
                    args = json.loads(item.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append(ToolCall(id=item.id, name=item.function.name,
                                      arguments=args if isinstance(args, dict) else {}))
        usage = getattr(response, "usage", None)
        if usage is not None and getattr(usage, "total_tokens", None) is not None:
            tokens, estimated = int(usage.total_tokens), False
        else:
            tokens, estimated = _estimate_tokens(sent, message.content), True
        return ChatReply(content=content, tool_calls=calls, total_tokens=tokens,
                         usage_estimated=estimated, used_json_fallback=used_json)
