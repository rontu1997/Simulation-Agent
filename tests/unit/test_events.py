import json
import time

from simagent.events import EventLog, Redactor, timed_tool

CANARY = "sk-canary-123456"


def test_redactor_replaces_canary_in_strings_and_nested_objects():
    red = Redactor([CANARY])
    assert red.redact(f"key={CANARY}!") == "key=***!"
    obj = {"a": [f"x {CANARY}", {"b": CANARY}], "n": 3}
    out = red.redact_obj(obj)
    assert CANARY not in json.dumps(out)
    assert out["n"] == 3


def test_short_secret_ignored():
    assert Redactor(["abc"]).redact("abc def") == "abc def"


def test_event_line_schema(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", Redactor([]))
    log.emit("info", "server", "tool_called", job_id="j1", stage="em", data={"k": 1})
    line = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert set(line) == {"ts", "level", "component", "event", "job_id", "stage", "data"}
    assert line["job_id"] == "j1" and line["data"] == {"k": 1}


def test_event_log_never_contains_secret(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", Redactor([CANARY]))
    log.emit("info", "llm", "request", data={"auth": f"Bearer {CANARY}"})
    assert CANARY not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")


def test_tool_time_excludes_external(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", Redactor([]))
    with timed_tool(log, "run_grompp", "j1") as timer:
        time.sleep(0.05)
        timer.add_external(0.04)
    line = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["event"] == "tool_done"
    assert line["data"]["tool"] == "run_grompp"
    assert 0 <= line["data"]["tool_time_s"] < 0.03
