"""Shared test doubles for the session and CLI tests: a scripted console and a stub model."""

import asyncio
from typing import Any

from simagent.llm import ChatReply, ToolCall


class ScriptedConsole:
    """Scripted input. An item may be ``(trigger, line)``: the line is released only after the
    output contains ``trigger``, so scripted answers arrive after their prompt, as a person's
    would. With ``release_on`` set, an empty script waits (a run is in progress) until that text
    has been printed, then reports end of input."""

    def __init__(self, inputs, release_on: str | None = None):
        self.inputs = list(inputs)
        self.out: list[str] = []
        self.release_on = release_on

    @property
    def transcript(self) -> str:
        return "\n".join(self.out)

    async def say(self, text: str) -> None:
        self.out.append(text)

    async def ask(self, prompt: str) -> str | None:
        if prompt:
            self.out.append(prompt)
        while True:
            if self.inputs:
                item = self.inputs[0]
                trigger, line = item if isinstance(item, tuple) else (None, item)
                if trigger is None or trigger in self.transcript:
                    self.inputs.pop(0)
                    return line
            elif not self.release_on or self.release_on in self.transcript:
                return None
            await asyncio.sleep(0.005)


class StubLlm:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent: list[list[dict[str, Any]]] = []

    def chat(self, messages, functions=None, max_tokens=1024):
        self.sent.append(list(messages))
        item = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        return item


def submit(**fields) -> ChatReply:
    origins = {k: "user" for k in fields}
    return ChatReply(content=None, total_tokens=10, tool_calls=[
        ToolCall("c1", "submit_request", {**fields, "origins": origins})])


def say(text: str) -> ChatReply:
    return ChatReply(content=text, total_tokens=10)
