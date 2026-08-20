from __future__ import annotations

import json
from pathlib import Path
import subprocess

from agent_relay.batch import run_batch
from agent_relay.result import WorkerResponse


def _make_repo(path: Path) -> None:
    path.mkdir()
    (path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _patch() -> str:
    return (
        "diff --git a/value.py b/value.py\n"
        "--- a/value.py\n"
        "+++ b/value.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )


class _Worker:
    def run(self, task, context, retry=None):
        return WorkerResponse(status="READY", summary=task.task_id, patch=_patch())


def test_batch_returns_compact_handoff_and_external_patch_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = tmp_path / "tasks.json"
    task = {
        "objective": "Set VALUE to 2.",
        "allowed_files": ["value.py"],
        "verification": ["py -3 -c \"import value; assert value.VALUE == 2\""],
    }
    manifest.write_text(
        json.dumps({
            "tasks": [
                {"task": {**task, "task_id": "one"}},
                {"task": {**task, "task_id": "two"}},
            ]
        }),
        encoding="utf-8",
    )
    artifacts = tmp_path / "artifacts"

    report = run_batch(
        manifest=manifest,
        repo=repo,
        artifact_dir=artifacts,
        worker_factory=lambda _repo, _model: _Worker(),
    )

    assert report["status"] == "PASS"
    assert report["review_mode"] == "compact-handoff"
    assert report["main_worktree_unchanged"] is True
    assert report["frontier_handoff_tokens_estimate"] > 0
    assert len(report["tasks"]) == 2
    assert all("-VALUE = 1" not in json.dumps(item) for item in report["tasks"])
    assert all(
        (Path(report["artifact_dir"]) / item["patch"]["artifact"]).is_file()
        for item in report["tasks"]
    )
    assert (repo / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_batch_aggregate_keeps_only_index_and_selected_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = tmp_path / "tasks.json"
    task = {
        "objective": "Set VALUE to 2.",
        "allowed_files": ["value.py"],
        "verification": ["py -3 -c \"import value; assert value.VALUE == 2\""],
    }
    manifest.write_text(
        json.dumps({
            "tasks": [
                {"task": {**task, "task_id": "one"}, "expected_status": "SUCCESS"},
                {"task": {**task, "task_id": "two"}, "expected_status": "SUCCESS"},
            ]
        }),
        encoding="utf-8",
    )

    report = run_batch(
        manifest=manifest,
        repo=repo,
        artifact_dir=tmp_path / "artifacts",
        aggregate=True,
        sample=1,
        worker_factory=lambda _repo, _model: _Worker(),
    )

    assert report["status"] == "PASS"
    assert report["review_mode"] == "aggregate-proof"
    assert report["tasks"] == []
    assert len(report["task_index"]["passed"]) == 2
    assert report["review_sample_count"] == 1
    assert report["frontier_budget"]["review_artifact_tokens_estimate"] > 0
    assert (tmp_path / "artifacts" / "full-records.json").is_file()


def test_batch_required_triage_runs_only_eligible_manifest_tasks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = tmp_path / "tasks.json"
    safe_task = {
        "task_id": "safe",
        "task_kind": "mechanical",
        "objective": "Set VALUE to 2.",
        "allowed_files": ["value.py"],
        "verification": ["py -3 -c \"import value; assert value.VALUE == 2\""],
    }
    kept_local_task = {
        **safe_task,
        "task_id": "keep-local",
        "task_kind": "architecture",
    }
    manifest.write_text(
        json.dumps({
            "tasks": [
                {
                    "task": safe_task,
                    "triage": {"avoided_tokens": 1800, "spent_tokens": 600},
                },
                {
                    "task": kept_local_task,
                    "triage": {"avoided_tokens": 1800, "spent_tokens": 600},
                },
            ]
        }),
        encoding="utf-8",
    )
    calls: list[str] = []

    class CountingWorker(_Worker):
        def run(self, task, context, retry=None):
            calls.append(task.task_id)
            return super().run(task, context, retry)

    report = run_batch(
        manifest=manifest,
        repo=repo,
        artifact_dir=tmp_path / "artifacts",
        require_triage=True,
        worker_factory=lambda _repo, _model: CountingWorker(),
    )

    assert report["status"] == "FAIL"
    assert calls == ["safe"]
    assert report["triage"]["required"] is True
    assert report["triage"]["delegated_tasks"] == 1
    assert report["triage"]["not_delegated_tasks"] == 1
    rejected = report["tasks"][1]
    assert rejected["status"] == "KEEP_LOCAL"
    assert rejected["not_delegated"] is True
    assert rejected["triage"]["decision"] == "KEEP_LOCAL"


def test_batch_required_triage_can_expect_a_blocked_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    manifest = tmp_path / "tasks.json"
    task = {
        "task_id": "incomplete",
        "objective": "Set VALUE to 2.",
        "allowed_files": ["value.py"],
        "verification": ["py -3 -c \"import value; assert value.VALUE == 2\""],
    }
    manifest.write_text(
        json.dumps({
            "tasks": [{
                "task": task,
                "expected_status": "BLOCKED",
                "triage": {"avoided_tokens": 1800, "spent_tokens": 600},
            }]
        }),
        encoding="utf-8",
    )
    calls: list[str] = []

    class CountingWorker(_Worker):
        def run(self, task, context, retry=None):
            calls.append(task.task_id)
            return super().run(task, context, retry)

    report = run_batch(
        manifest=manifest,
        repo=repo,
        artifact_dir=tmp_path / "artifacts",
        require_triage=True,
        worker_factory=lambda _repo, _model: CountingWorker(),
    )

    assert report["status"] == "PASS"
    assert calls == []
    assert report["tasks"][0]["status"] == "BLOCKED"
    assert report["tasks"][0]["expected_status"] == "BLOCKED"
