"""The S3 history sync: merging never loses or duplicates a record, push and pull move data the right way, and a missing end never breaks the other."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("history_sync", ROOT / "scripts" / "aws" / "history_sync.py")
hs = importlib.util.module_from_spec(_spec)
sys.modules["history_sync"] = hs
_spec.loader.exec_module(hs)


def lines(*recs):
    return "".join(json.dumps(r) + "\n" for r in recs)


def ids(text):
    return [json.loads(l)["id"] for l in text.splitlines()]


A = {"id": "INC-0001", "received_at": "2026-11-20T10:00:00+00:00"}
B = {"id": "INC-0002", "received_at": "2026-11-20T11:00:00+00:00"}
C = {"id": "INC-0003", "received_at": "2026-11-20T12:00:00+00:00"}


def test_merge_is_a_union_without_duplicates_and_oldest_first():
    assert ids(hs.merge_jsonl(lines(A, B), lines(B, C))) == ["INC-0001", "INC-0002", "INC-0003"]
    assert ids(hs.merge_jsonl(lines(C), lines(A))) == ["INC-0001", "INC-0003"]


def test_merge_drops_a_half_written_line_and_keeps_records_without_an_id():
    broken = lines(A) + '{"id": "INC-0002", "recei'
    assert ids(hs.merge_jsonl(broken, lines(B))) == ["INC-0001", "INC-0002"]
    noid = {"t": "2026-11-20T09:00:00+00:00", "text": "x"}
    assert hs.merge_jsonl(lines(noid), lines(noid)).count("\n") == 1


def test_merging_with_itself_changes_nothing():
    once = hs.merge_jsonl(lines(A, B, C))
    assert hs.merge_jsonl(once, once) == once and hs.merge_jsonl(once) == once


class World:
    """A fake cluster and bucket: files in dicts, commands routed by their first words."""
    def __init__(self, pod=None, bucket=None, down=()):
        self.pod, self.bucket, self.down, self.syncs = dict(pod or {}), dict(bucket or {}), set(down), []

    def run(self, cmd, stdin=None):
        if cmd[0] == "kubectl":
            deploy = next(c for c in cmd if c.startswith("deploy/")).split("/")[1]
            if deploy in self.down:
                return 1, ""
            if "-i" in cmd:
                path = cmd[-1].split("cat > ")[1].split(".new")[0]
                self.pod[(deploy, path)] = stdin
                return 0, ""
            path = cmd[-1].split("cat ")[1].split(" ")[0]
            return 0, self.pod.get((deploy, path), "")
        if cmd[:3] == ["aws", "s3", "cp"]:
            if cmd[3] == "-":
                self.bucket[cmd[4]] = stdin
                return 0, ""
            return (0, self.bucket[cmd[3]]) if cmd[3] in self.bucket else (1, "")
        if cmd[:3] == ["aws", "s3", "sync"]:
            self.syncs.append(cmd[3:5])
            return 0, ""
        raise AssertionError(cmd)


def sync(world, tmp_path):
    return hs.Sync("bkt", "autosre", world.run, tmp_path / "results", log=lambda *_: None)


def test_push_uploads_what_the_cluster_has_and_never_shrinks_the_bucket(tmp_path):
    w = World(pod={("correlation-engine", "/data/incidents.jsonl"): lines(B, C)}, bucket={"s3://bkt/autosre/history/incidents.jsonl": lines(A, B)})
    (tmp_path / "results").mkdir()
    report = sync(w, tmp_path).push()
    assert ids(w.bucket["s3://bkt/autosre/history/incidents.jsonl"]) == ["INC-0001", "INC-0002", "INC-0003"]      # the bucket kept A, gained C
    assert report["incidents.jsonl"].startswith("uploaded 3") and report["results"] == "synced" and w.syncs


def test_a_second_push_with_nothing_new_uploads_nothing(tmp_path):
    w = World(pod={("correlation-engine", "/data/incidents.jsonl"): lines(A, B)}, bucket={"s3://bkt/autosre/history/incidents.jsonl": lines(A, B)})
    assert sync(w, tmp_path).push()["incidents.jsonl"].startswith("unchanged")


def test_pull_restores_an_empty_cluster_from_the_bucket_after_a_lab_restart(tmp_path):
    w = World(bucket={"s3://bkt/autosre/history/incidents.jsonl": lines(A, B, C), "s3://bkt/autosre/history/executions.jsonl": lines({"id": "abc", "started_at": "2026-11-20T10:00:05+00:00"})})
    report = sync(w, tmp_path).pull()
    assert ids(w.pod[("correlation-engine", "/data/incidents.jsonl")]) == ["INC-0001", "INC-0002", "INC-0003"]
    assert ids(w.pod[("remediation-dispatcher", "/data/executions.jsonl")]) == ["abc"]
    assert report["restart_needed"] is True and report["events.jsonl"] == "nothing in the bucket"


def test_pull_keeps_records_the_cluster_has_that_the_bucket_lacks(tmp_path):
    w = World(pod={("correlation-engine", "/data/incidents.jsonl"): lines(C)}, bucket={"s3://bkt/autosre/history/incidents.jsonl": lines(A, B)})
    sync(w, tmp_path).pull()
    assert ids(w.pod[("correlation-engine", "/data/incidents.jsonl")]) == ["INC-0001", "INC-0002", "INC-0003"]


def test_pull_that_changes_nothing_does_not_ask_for_a_restart(tmp_path):
    w = World(pod={("correlation-engine", "/data/incidents.jsonl"): lines(A, B)}, bucket={"s3://bkt/autosre/history/incidents.jsonl": lines(A, B)})
    assert sync(w, tmp_path).pull()["restart_needed"] is False


def test_an_unreachable_service_is_skipped_and_the_others_still_sync(tmp_path):
    w = World(pod={("remediation-dispatcher", "/data/executions.jsonl"): lines({"id": "x", "started_at": "2026-11-20T10:00:00+00:00"})}, down={"correlation-engine"})
    report = sync(w, tmp_path).push()
    assert report["incidents.jsonl"] == "unreachable" and report["executions.jsonl"].startswith("uploaded 1")
