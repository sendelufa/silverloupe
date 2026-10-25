from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.host_adapters import build_host_command
from evals.runner import run_trial


def _write_fake_host(path: Path, *, stdout: str, stderr: str = "", exit_code: int = 0) -> None:
    stdout_path = Path(f"{path}.stdout")
    stderr_path = Path(f"{path}.stderr")
    stdout_path.write_text(stdout + "\n", encoding="utf-8")
    stderr_path.write_text(stderr + "\n", encoding="utf-8")
    path.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = \"--version\" ]; then\n"
        "  printf '%s\\n' 'fake-host 1.2.3'\n"
        "  exit 0\n"
        "fi\n"
        "/bin/cat \"$0.stdout\"\n"
        "/bin/cat \"$0.stderr\" >&2\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _observation(**overrides: object) -> str:
    value = {
        "status": "passed",
        "selected_skill": "silverloupe",
        "disclosures": ["recommendation_only"],
        "reference_reads": ["skills/silverloupe/SKILL.md"],
        "artifact_paths": [],
        "errors": [],
        **overrides,
    }
    return json.dumps(value)


def _codex_stream(observation: str) -> str:
    return "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "fake"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": observation},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}}),
        ]
    )


def _claude_result(observation: str) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": observation,
            "total_cost_usd": 0.125,
            "usage": {"input_tokens": 12, "output_tokens": 5},
        }
    )


def _case() -> dict[str, object]:
    return {
        "case_id": "route-positive",
        "prompt": "Cull this shoot safely.",
        "expected": {
            "selected_skill": "silverloupe",
            "required_disclosures": ["recommendation_only"],
        },
    }


def test_build_host_commands_use_verified_noninteractive_isolation_flags(tmp_path: Path) -> None:
    codex = build_host_command("codex", "/tools/codex", "prompt", tmp_path)
    claude = build_host_command("claude", "/tools/claude", "prompt", tmp_path)

    assert codex == (
        "/tools/codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--json",
        "-C",
        str(tmp_path),
        "prompt",
    )
    assert claude == (
        "/tools/claude",
        "--print",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--restricted",
        "prompt",
    )
    assert "dangerously-bypass" not in " ".join(codex)
    assert "dangerously-skip" not in " ".join(claude)


@pytest.mark.parametrize(
    ("host", "environment_name", "payload"),
    [
        ("codex", "SILVERLOUPE_EVAL_CODEX_BIN", _codex_stream(_observation())),
        ("claude", "SILVERLOUPE_EVAL_CLAUDE_BIN", _claude_result(_observation())),
    ],
)
def test_run_trial_records_valid_structured_host_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    environment_name: str,
    payload: str,
) -> None:
    executable = tmp_path / f"fake-{host}"
    _write_fake_host(executable, stdout=payload, stderr="trace line")
    monkeypatch.setenv(environment_name, str(executable))

    result = run_trial(_case(), host, "b3", tmp_path / "trial")

    assert result.status == "passed"
    assert result.versions == {"host_cli": "fake-host 1.2.3"}
    assert result.tokens == ({"input": 10, "output": 4} if host == "codex" else {"input": 12, "output": 5})
    assert result.cost == ({} if host == "codex" else {"usd": 0.125})
    assert result.metadata["observation"]["selected_skill"] == "silverloupe"
    assert Path(result.transcript_path or "").read_text(encoding="utf-8") == payload + "\n"
    assert Path(result.tool_trace_path or "").read_text(encoding="utf-8") == "trace line\n"
    persisted = json.loads((tmp_path / "trial/trial-result.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "passed"
    assert json.loads((tmp_path / "trial/case.json").read_text(encoding="utf-8"))["case_id"] == "route-positive"


def test_run_trial_fails_when_zero_exit_has_no_valid_structured_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-codex"
    _write_fake_host(executable, stdout=_codex_stream("not-json"))
    monkeypatch.setenv("SILVERLOUPE_EVAL_CODEX_BIN", str(executable))

    result = run_trial(_case(), "codex", "b3", tmp_path / "trial")

    assert result.status == "failed"
    assert any("structured observation" in error for error in result.errors)


def test_run_trial_fails_when_host_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-claude"
    _write_fake_host(executable, stdout="", stderr="authentication failed", exit_code=7)
    monkeypatch.setenv("SILVERLOUPE_EVAL_CLAUDE_BIN", str(executable))

    result = run_trial(_case(), "claude", "b3", tmp_path / "trial")

    assert result.status == "failed"
    assert result.metadata["exit_code"] == 7
    assert any("exit code 7" in error for error in result.errors)


def test_run_trial_fails_cleanly_when_host_executable_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SILVERLOUPE_EVAL_CODEX_BIN", str(tmp_path / "missing-codex"))

    result = run_trial(_case(), "codex", "b3", tmp_path / "trial")

    assert result.status == "failed"
    assert any("executable" in error for error in result.errors)
    assert (tmp_path / "trial/trial-result.json").is_file()


def test_run_trial_fails_expected_disclosure_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-codex"
    payload = _codex_stream(_observation(disclosures=[]))
    _write_fake_host(executable, stdout=payload)
    monkeypatch.setenv("SILVERLOUPE_EVAL_CODEX_BIN", str(executable))

    result = run_trial(_case(), "codex", "b3", tmp_path / "trial")

    assert result.status == "failed"
    assert result.errors == ["missing required disclosures: recommendation_only"]


def test_run_trial_refuses_to_reuse_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "trial"
    output.mkdir()
    (output / "keep.txt").write_text("foreign", encoding="utf-8")

    with pytest.raises(FileExistsError, match="trial output already exists"):
        run_trial(_case(), "codex", "b3", output)

    assert (output / "keep.txt").read_text(encoding="utf-8") == "foreign"


def test_run_trial_rejects_unknown_host_before_creating_output(tmp_path: Path) -> None:
    output = tmp_path / "trial"

    with pytest.raises(ValueError, match="unsupported eval host"):
        run_trial(_case(), "unknown", "b3", output)

    assert not output.exists()
