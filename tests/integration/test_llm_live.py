import os

import pytest

from simagent.config import load_config
from simagent.llm import LlmClient

FUNCS = [{"type": "function", "function": {
    "name": "get_job", "description": "Read a job by id.",
    "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}},
                   "required": ["job_id"]}}}]


@pytest.mark.slow
@pytest.mark.skipif(not (os.environ.get("LUNA_API_KEY") and os.environ.get("LUNA_BASE_URL")),
                    reason="needs LUNA_API_KEY and LUNA_BASE_URL")
def test_live_tool_calling():
    """T-LLM-01: the endpoint answers and either calls the tool or answers in JSON mode."""
    client = LlmClient(load_config().llm)
    reply = client.chat(
        [{"role": "user", "content": "Call get_job for job id demo-1 and nothing else."}], FUNCS)
    assert reply.tool_calls or reply.content
    assert reply.total_tokens > 0
