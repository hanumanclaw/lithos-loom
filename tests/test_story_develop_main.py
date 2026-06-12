"""CLI-boundary validation tests for the story-develop entry point.

These exercise ``main()``'s fail-fast guards, which return before any Docker /
agent work happens.
"""

from __future__ import annotations

from pathlib import Path

from lithos_loom.plugins.story_develop.__main__ import main


def test_main_rejects_empty_description(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--description", "   "])
    assert rc == 2
    assert "description must not be empty" in capsys.readouterr().err


def test_main_rejects_non_git_repo(tmp_path: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_path), "--description", "do a thing"])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err


def test_main_rejects_invalid_reviewer_name(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "code quality",
        ]
    )
    assert rc == 2
    assert "invalid --reviewer" in capsys.readouterr().err


def test_main_rejects_bad_max_rounds(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--description", "x", "--max-rounds", "0"])
    assert rc == 2
    assert "--max-rounds must be >= 1" in capsys.readouterr().err


def test_main_rejects_duplicate_reviewers(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "cq",
            "--reviewer",
            "cq",
        ]
    )
    assert rc == 2
    assert "duplicate --reviewer" in capsys.readouterr().err


def test_main_rejects_reviewer_with_develop_config(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    cfg = tmp_path / "develop.toml"
    cfg.write_text("[[reviewers]]\nname = 'cq'\n")
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "other",
            "--develop-config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_rejects_bad_develop_config(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    cfg = tmp_path / "develop.toml"
    cfg.write_text("[[reviewers]]\nname = 'Bad Name'\n")
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--develop-config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert "must be a lowercase" in capsys.readouterr().err


def test_main_rejects_zero_pause_poll(tmp_git_repo: Path, capsys) -> None:
    # 0 would spin forever on zero-second pauses; negative would crash sleep()
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--pause-poll-minutes",
            "0",
        ]
    )
    assert rc == 2
    assert "--pause-poll-minutes must be >= 1" in capsys.readouterr().err


def test_main_rejects_bad_max_cost(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        ["--repo", str(tmp_git_repo), "--description", "x", "--max-cost-usd", "0"]
    )
    assert rc == 2
    assert "--max-cost-usd must be > 0" in capsys.readouterr().err


def test_main_rejects_negative_max_pause(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--max-pause-minutes",
            "-1",
        ]
    )
    assert rc == 2
    assert "--max-pause-minutes must be >= 0" in capsys.readouterr().err


def test_main_rejects_task_id_with_no_lithos(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--task-id", "t-1", "--no-lithos"])
    assert rc == 2
    assert "incompatible" in capsys.readouterr().err


def test_main_requires_description_or_task_id(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo)])
    assert rc == 2
    assert "one of --description or --task-id" in capsys.readouterr().err


def test_main_rejects_missing_ac_file(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--acceptance-criteria",
            "@/nonexistent/ac.md",
        ]
    )
    assert rc == 2
    assert "cannot read --acceptance-criteria" in capsys.readouterr().err


def test_main_rejects_blank_ac(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--acceptance-criteria",
            "  ",
        ]
    )
    assert rc == 2
    assert "--acceptance-criteria must not be empty" in capsys.readouterr().err


def test_main_task_id_resolves_description_and_posts(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """--task-id alone: task text becomes the description, metadata AC flows
    into the config, and results are posted back after the run."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult
    from lithos_loom.plugins.story_develop.lithos_io import TaskContext

    captured: dict = {}

    def fake_fetch(url, task_id):
        captured["fetched"] = (url, task_id)
        return TaskContext(
            task_id=task_id,
            title="Add a flag",
            description="Body.",
            acceptance_criteria="must have tests",
            metadata={},
        )

    def fake_develop(config, **kw):
        captured["config"] = config
        return DevelopResult(
            status="approved",
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.1,
            review_cost_usd=0.1,
            message="ok",
        )

    def fake_post(url, task_id, result):
        captured["posted"] = (url, task_id, result.status)
        return True

    monkeypatch.setattr(main_mod, "fetch_task_context", fake_fetch)
    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(main_mod, "post_results", fake_post)

    rc = main_mod.main(["--repo", str(tmp_git_repo), "--task-id", "t-9"])
    assert rc == 0
    assert captured["fetched"][1] == "t-9"
    cfg = captured["config"]
    assert cfg.description == "Add a flag\n\nBody."
    assert cfg.acceptance_criteria == "must have tests"
    assert captured["posted"] == ("http://localhost:8765", "t-9", "approved")
    out = capsys.readouterr().out
    assert "developing Lithos task t-9" in out
    assert "results posted to task t-9" in out


def test_main_rejects_task_id_with_description(tmp_git_repo: Path, capsys) -> None:
    # The task IS the description — a mixed source would let the audit trail
    # claim task X while developing unrelated text.
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--task-id",
            "t-1",
            "--description",
            "something else entirely",
        ]
    )
    assert rc == 2
    assert "--task-id and --description are incompatible" in capsys.readouterr().err


def test_main_rejects_complete_on_approval_without_task_id(
    tmp_git_repo: Path, capsys
) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--complete-on-approval",
        ]
    )
    assert rc == 2
    assert "--complete-on-approval requires --task-id" in capsys.readouterr().err


def test_main_complete_on_approval_completes_task(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult
    from lithos_loom.plugins.story_develop.lithos_io import TaskContext

    captured: dict = {}

    def fake_fetch(url, task_id):
        return TaskContext(
            task_id=task_id,
            title="T",
            description="",
            acceptance_criteria=None,
            metadata={},
        )

    def _fake_result(status: str) -> DevelopResult:
        return DevelopResult(
            status=status,
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.1,
            review_cost_usd=0.1,
            message="m",
        )

    monkeypatch.setattr(main_mod, "fetch_task_context", fake_fetch)
    monkeypatch.setattr(main_mod, "post_results", lambda *a: True)
    monkeypatch.setattr(
        main_mod, "complete_task", lambda *a: captured.setdefault("completed", True)
    )

    # approved + flag -> completes
    monkeypatch.setattr(main_mod, "develop", lambda c, **kw: _fake_result("approved"))
    rc = main_mod.main(
        ["--repo", str(tmp_git_repo), "--task-id", "t-1", "--complete-on-approval"]
    )
    assert rc == 0 and captured.get("completed") is True
    assert "marked completed" in capsys.readouterr().out

    # NOT approved + flag -> no completion
    captured.clear()
    monkeypatch.setattr(main_mod, "develop", lambda c, **kw: _fake_result("stalled"))
    rc = main_mod.main(
        ["--repo", str(tmp_git_repo), "--task-id", "t-1", "--complete-on-approval"]
    )
    assert rc == 1 and "completed" not in captured

    # approved WITHOUT the flag -> no completion (default behaviour)
    captured.clear()
    monkeypatch.setattr(main_mod, "develop", lambda c, **kw: _fake_result("approved"))
    rc = main_mod.main(["--repo", str(tmp_git_repo), "--task-id", "t-1"])
    assert rc == 0 and "completed" not in captured
