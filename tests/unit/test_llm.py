import json
from dataclasses import replace

import pytest

try:
    import httpx2 as httpx  # openai 3.x ships its own HTTP library
except ImportError:  # openai 2.x
    import httpx

from simagent.config import LlmConfig
from simagent.llm import LlmClient, LlmUnavailable

KEY = "sk-test-key-98765"
CFG = LlmConfig(model="gpt6-luna", base_url="http://llm.test/v1", api_key=KEY, temperature=0.1)
FUNCS = [{"type": "function", "function": {
    "name": "get_job", "description": "read a job",
    "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}}}}]


def completion(content="hello", tool_calls=None, usage=True):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body = {"id": "x", "object": "chat.completion", "created": 1, "model": "gpt6-luna",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
    if usage:
        body["usage"] = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
    return httpx.Response(200, json=body)


def error(status, message):
    return httpx.Response(status, json={"error": {"message": message,
                                                  "type": "invalid_request_error"}})


class Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return item(request) if callable(item) else item

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def client(server, cfg=CFG, sleeps=None):
    http = httpx.Client(transport=httpx.MockTransport(server))
    return LlmClient(cfg, http_client=http,
                     sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


MSGS = [{"role": "user", "content": "hi there"}]


def test_plain_reply():
    reply = client(Server(completion("hello"))).chat(MSGS)
    assert reply.content == "hello" and reply.tool_calls == []
    assert reply.total_tokens == 7 and reply.usage_estimated is False
    assert reply.used_json_fallback is False


def test_tool_calls_parsed():
    calls = [{"id": "call_1", "type": "function",
              "function": {"name": "get_job", "arguments": "{\"job_id\": \"j\"}"}}]
    reply = client(Server(completion(None, tool_calls=calls))).chat(MSGS, FUNCS)
    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("call_1", "get_job", {"job_id": "j"})


def test_authorization_header_uses_key():
    server = Server(completion())
    client(server).chat(MSGS)
    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert str(server.requests[0].url).startswith("http://llm.test/v1/chat/completions")


def test_400_naming_temperature_retries_without_it_once_and_remembers():
    server = Server(error(400, "Unsupported parameter: 'temperature'"), completion())
    c = client(server)
    c.chat(MSGS)
    c.chat(MSGS)
    bodies = server.bodies()
    assert "temperature" in bodies[0]
    assert "temperature" not in bodies[1] and "temperature" not in bodies[2]
    assert len(bodies) == 3


def test_400_naming_max_tokens_retries_with_max_completion_tokens():
    server = Server(error(400, "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'"),
                    completion())
    client(server).chat(MSGS, max_tokens=321)
    first, second = server.bodies()
    assert first["max_tokens"] == 321
    assert "max_tokens" not in second and second["max_completion_tokens"] == 321


def test_429_backoff_waits_1_2_4_8():
    server = Server(*[error(429, "slow down")] * 4, completion())
    sleeps: list[float] = []
    reply = client(server, sleeps=sleeps).chat(MSGS)
    assert reply.content == "hello"
    assert sleeps == [1, 2, 4, 8]
    assert len(server.requests) == 5


def test_5xx_exhausts_retries_raises_LlmUnavailable():
    server = Server(error(500, "boom"))
    sleeps: list[float] = []
    with pytest.raises(LlmUnavailable):
        client(server, sleeps=sleeps).chat(MSGS)
    assert sleeps == [1, 2, 4, 8]
    assert len(server.requests) == 5


def test_missing_usage_is_estimated():
    messages = [{"role": "user", "content": "x" * 40}]
    reply = client(Server(completion("y" * 20, usage=False))).chat(messages)
    assert reply.usage_estimated is True
    assert reply.total_tokens == 15  # (40 + 20) characters / 4


def test_tools_rejected_uses_json_fallback():
    json_reply = json.dumps({"tool": "get_job", "args": {"job_id": "j"}, "message": "m"})
    server = Server(error(400, "tools is not supported by this model"), completion(json_reply))
    reply = client(server).chat(MSGS, FUNCS)
    first, second = server.bodies()
    assert "tools" in first and "tools" not in second
    assert second["response_format"] == {"type": "json_object"}
    assert "get_job" in json.dumps(second["messages"])
    assert reply.used_json_fallback is True
    assert [(c.name, c.arguments) for c in reply.tool_calls] == [("get_job", {"job_id": "j"})]
    assert reply.content == "m"


def test_json_fallback_reply_without_tool():
    server = Server(error(400, "tools unsupported"),
                    completion(json.dumps({"tool": None, "args": {}, "message": "just talking"})))
    reply = client(server).chat(MSGS, FUNCS)
    assert reply.tool_calls == [] and reply.content == "just talking"


def test_error_never_contains_key():
    server = Server(error(500, f"upstream said {KEY}"))
    with pytest.raises(LlmUnavailable) as exc:
        client(server).chat(MSGS)
    assert KEY not in str(exc.value)


def test_unexpected_bad_request_raises():
    with pytest.raises(LlmUnavailable):
        client(Server(error(400, "context length exceeded"))).chat(MSGS)


def test_config_temperature_is_sent():
    server = Server(completion())
    client(server, cfg=replace(CFG, temperature=0.3)).chat(MSGS)
    assert server.bodies()[0]["temperature"] == 0.3
