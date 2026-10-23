import json
import threading

from simagent.core.paths import sha256_file
from simagent.core.provenance import Ledger, file_entry, record_ack, record_internal


def test_seq_monotonic(tmp_path):
    ledger = Ledger(tmp_path / "provenance.jsonl")
    assert [ledger.append({"event": "start"}) for _ in range(3)] == [1, 2, 3]
    assert [r["seq"] for r in ledger.records()] == [1, 2, 3]
    assert all("ts" in r for r in ledger.records())


def test_concurrent_appends_unique_seq(tmp_path):
    path = tmp_path / "provenance.jsonl"

    def worker() -> None:
        ledger = Ledger(path)  # separate instance per thread, like separate processes
        for _ in range(25):
            ledger.append({"event": "start"})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == list(range(1, 201))


def test_append_only_api():
    for name in ("update", "delete", "remove", "rewrite", "edit", "truncate"):
        assert not hasattr(Ledger, name)


def test_record_internal_has_receipt(tmp_path):
    base = tmp_path / "job"
    (base / "01_build").mkdir(parents=True)
    f = base / "01_build" / "topol.top"
    f.write_text("[ system ]\n", encoding="utf-8")
    ledger = Ledger(base / "provenance.jsonl")
    record_internal(ledger, stage="build", tool="build_water_box", base=base,
                    outputs=["01_build/topol.top"])
    sha = sha256_file(f)
    assert ledger.has_receipt("01_build/topol.top", sha)
    assert not ledger.has_receipt("01_build/topol.top", "0" * 64)
    assert not ledger.has_receipt("01_build/other.top", sha)
    rec = ledger.records()[-1]
    assert rec["kind"] == "internal" and rec["event"] == "end" and rec["stage"] == "build"


def test_record_ack_shape(tmp_path):
    ledger = Ledger(tmp_path / "provenance.jsonl")
    seq = record_ack(ledger, warning_id="abc123def456", text="WARNING 1", note="checked")
    rec = ledger.records()[0]
    assert seq == 1
    assert rec["event"] == "ack" and rec["kind"] == "internal"
    assert rec["warning_id"] == "abc123def456" and rec["text"] == "WARNING 1"
    assert rec["note"] == "checked"


def test_file_entry_hash(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"abc")
    entry = file_entry(tmp_path, "a.txt")
    assert entry == {"path": "a.txt", "bytes": 3, "sha256": sha256_file(tmp_path / "a.txt")}


def test_latest_end(tmp_path):
    ledger = Ledger(tmp_path / "p.jsonl")
    ledger.append({"event": "start", "stage": "em"})
    ledger.append({"event": "end", "stage": "em", "exit_code": 0})
    ledger.append({"event": "end", "stage": "nvt", "exit_code": 1})
    assert ledger.latest_end("em")["exit_code"] == 0
    assert ledger.latest_end("npt") is None
