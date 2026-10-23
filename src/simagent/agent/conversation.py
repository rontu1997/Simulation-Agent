"""The conversation loop (design 14.1, 14.3, 14.5): the model proposes, code decides.

The model is offered the read-only tools and two internal functions, and nothing else. A call to
any other name is answered locally with ``E_NOT_ALLOWED`` and never reaches the server, however
the model came to ask for it (RULE-X-14). Tool results go back compacted, and text inside them is
data, not instructions (RULE-SEC-04).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from mcp.types import Tool

from simagent.agent.controller import ToolCaller
from simagent.agent.request import ASK_RESEARCHER_FUNCTION, SUBMIT_REQUEST_FUNCTION, Question
from simagent.config import Config
from simagent.llm import ChatReply, LlmUnavailable, ToolCall
from simagent.mcp_client import to_openai_functions

CONTEXT_MESSAGES = 12
MAX_LOG_LINES = 20
MAX_FIELD_CHARS = 2000
MAX_RESULT_CHARS = 6000
MAX_QUESTIONS = 3


class ChatClient(Protocol):
    def chat(self, messages: list[dict[str, Any]],
             functions: Sequence[dict[str, Any]] | None = None,
             max_tokens: int = 1024) -> ChatReply: ...


@dataclass
class TurnResult:
    text: str | None
    submission: dict[str, Any] | None = None
    questions: list[Question] = field(default_factory=list)
    stop: str | None = None  # token_budget, tool_cap, llm_unavailable


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD_CHARS else value[:MAX_FIELD_CHARS] + "…"
    if isinstance(value, list):
        return [_clip(v) for v in value]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    return value


def compact(result: dict[str, Any]) -> str:
    """A tool envelope as JSON for the model: log tails of 20 lines, long fields clipped, and a
    hard size limit (design 9.3)."""
    data = json.loads(json.dumps(result))
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("lines"), list):
        inner["lines"] = inner["lines"][-MAX_LOG_LINES:]
    text = json.dumps(_clip(data))
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return json.dumps({"ok": result.get("ok"), "truncated": True,
                       "preview": text[:MAX_RESULT_CHARS - 200]})


def _error(code: str, message: str, hint: str = "") -> dict[str, Any]:
    return {"ok": False, "data": {}, "error": {"code": code, "message": message, "hint": hint}}


class Conversation:
    def __init__(self, llm: ChatClient, tools: ToolCaller, cfg: Config, *, system_prompt: str,
                 read_tools: Sequence[Tool],
                 job_summary: Callable[[], str] | None = None) -> None:
        self.llm = llm
        self.tools = tools
        self.cfg = cfg
        self.system_prompt = system_prompt
        self.job_summary = job_summary
        self.functions = [*to_openai_functions(read_tools), SUBMIT_REQUEST_FUNCTION,
                          ASK_RESEARCHER_FUNCTION]
        self.function_names = [f["function"]["name"] for f in self.functions]
        self.history: list[dict[str, Any]] = []
        self.tokens_used = 0
        self._budget_text: str | None = None

    def _context(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        if self.job_summary is not None:
            messages.append({"role": "system",
                             "content": f"Job summary: {self.job_summary()}"})
        window = self.history[-CONTEXT_MESSAGES:]
        while window and window[0]["role"] != "user":  # never start on an orphan tool message
            window = window[1:]
        return messages + window

    def _halt_budget(self) -> TurnResult:
        budget = self.cfg.llm.token_budget
        self._budget_text = (f"The token budget for this session ({budget}) is used up, so I "
                             "am stopping here. Nothing running has been touched.")
        return TurnResult(self._budget_text, stop="token_budget")

    async def turn(self, user_text: str) -> TurnResult:
        if self._budget_text is not None:
            return TurnResult(self._budget_text, stop="token_budget")
        self.history.append({"role": "user", "content": user_text})
        cap = self.cfg.llm.max_tool_calls_per_turn
        calls = 0
        while True:
            if calls >= cap:
                return TurnResult(f"I stopped after {cap} tool calls in one turn (the limit). "
                                  "Ask again to continue.", stop="tool_cap")
            try:
                reply = await asyncio.to_thread(self.llm.chat, self._context(), self.functions,
                                                1024)
            except LlmUnavailable as exc:
                return TurnResult(f"The model is unavailable: {exc}", stop="llm_unavailable")
            self.tokens_used += reply.total_tokens
            if self.tokens_used >= self.cfg.llm.token_budget:
                return self._halt_budget()
            if not reply.tool_calls:
                self.history.append({"role": "assistant", "content": reply.content or ""})
                return TurnResult(reply.content)
            self.history.append({
                "role": "assistant", "content": reply.content,
                "tool_calls": [{"id": c.id, "type": "function", "function": {
                    "name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in reply.tool_calls]})
            submission: dict[str, Any] | None = None
            questions: list[Question] = []
            for tool_call in reply.tool_calls:
                calls += 1
                result, submitted, asked = await self._execute(tool_call, calls > cap)
                submission = submitted if submitted is not None else submission
                questions = asked or questions
                self.history.append({"role": "tool", "tool_call_id": tool_call.id,
                                     "content": compact(result)})
            if submission is not None or questions:
                return TurnResult(reply.content, submission, questions)

    async def _execute(self, call: ToolCall, over_cap: bool
                       ) -> tuple[dict[str, Any], dict[str, Any] | None, list[Question]]:
        if over_cap:
            return _error("E_TOOL_CAP", "too many tool calls in this turn"), None, []
        if call.name not in self.function_names:
            return (_error("E_NOT_ALLOWED", f"{call.name} is not available to you",
                           "you can read job state and submit the request; nothing else"),
                    None, [])
        args = call.arguments if isinstance(call.arguments, dict) else {}
        if call.name == "submit_request":
            return {"ok": True, "data": {"received": True}, "error": None}, dict(args), []
        if call.name == "ask_researcher":
            return ({"ok": True, "data": {"asked": True}, "error": None}, None,
                    _questions(args))
        try:
            return await self.tools.call(call.name, args), None, []
        except Exception as exc:  # the server went away; tell the model, do not crash
            return _error("E_TOOL_FAILED", f"{call.name} failed: {type(exc).__name__}"), None, []


def _questions(args: dict[str, Any]) -> list[Question]:
    found: list[Question] = []
    for item in args.get("questions") or []:
        if isinstance(item, dict) and all(isinstance(item.get(k), str)
                                          for k in ("field", "text", "proposal")):
            found.append(Question(item["field"], item["text"], item["proposal"]))
    return found[:MAX_QUESTIONS]
