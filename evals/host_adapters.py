"""Verified non-interactive adapters for development-only host trials."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from evals.contracts import HostObservation


SUPPORTED_HOSTS = ("codex", "claude")
_EXECUTABLE_ENV = {
    "codex": "SILVERLOUPE_EVAL_CODEX_BIN",
    "claude": "SILVERLOUPE_EVAL_CLAUDE_BIN",
}


@dataclass(frozen=True)
class HostExecution:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    wall_time_seconds: float
    version: str | None = None
    observation: HostObservation | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


def _require_host(host: str) -> None:
    if host not in SUPPORTED_HOSTS:
        raise ValueError(f"unsupported eval host: {host}")


def resolve_host_executable(host: str) -> str | None:
    """Resolve an explicitly configured host binary before consulting PATH."""

    _require_host(host)
    configured = os.environ.get(_EXECUTABLE_ENV[host])
    if configured is not None:
        return configured
    return shutil.which(host)


def build_host_command(
    host: str, executable: str, prompt: str, workspace: Path
) -> tuple[str, ...]:
    """Build the reviewed, non-interactive command for one host."""

    _require_host(host)
    if host == "codex":
        return (
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--json",
            "-C",
            str(workspace),
            prompt,
        )
    return (
        executable,
        "--print",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--permission-mode",
        "dontAsk",
        "--restricted",
        prompt,
    )


def _version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def _observation(value: str) -> tuple[HostObservation | None, list[str]]:
    try:
        return HostObservation.model_validate_json(value), []
    except ValidationError as error:
        return None, [f"host did not return a valid structured observation: {error}"]


def _parse_codex(stdout: str) -> tuple[HostObservation | None, dict[str, int], dict[str, float], list[str]]:
    final_text: str | None = None
    tokens: dict[str, int] = {}
    errors: list[str] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(f"codex event line {line_number} is invalid JSON: {error.msg}")
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    final_text = text
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
            if isinstance(usage.get("input_tokens"), int):
                tokens["input"] = usage["input_tokens"]
            if isinstance(usage.get("output_tokens"), int):
                tokens["output"] = usage["output_tokens"]
    if final_text is None:
        errors.append("host did not return a structured observation")
        return None, tokens, {}, errors
    observation, parse_errors = _observation(final_text)
    return observation, tokens, {}, [*errors, *parse_errors]


def _parse_claude(stdout: str) -> tuple[HostObservation | None, dict[str, int], dict[str, float], list[str]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        return None, {}, {}, [f"claude result is invalid JSON: {error.msg}"]
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
        return None, {}, {}, ["host did not return a structured observation"]
    tokens: dict[str, int] = {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        if isinstance(usage.get("input_tokens"), int):
            tokens["input"] = usage["input_tokens"]
        if isinstance(usage.get("output_tokens"), int):
            tokens["output"] = usage["output_tokens"]
    cost: dict[str, float] = {}
    total_cost = payload.get("total_cost_usd")
    if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool):
        cost["usd"] = float(total_cost)
    observation, errors = _observation(payload["result"])
    return observation, tokens, cost, errors


def execute_host(host: str, prompt: str, workspace: Path) -> HostExecution:
    """Invoke one host and parse only observable, structured evidence."""

    _require_host(host)
    executable = resolve_host_executable(host)
    if executable is None or not Path(executable).is_file():
        return HostExecution(
            command=(),
            exit_code=None,
            stdout="",
            stderr="",
            wall_time_seconds=0.0,
            errors=(f"{host} executable was not found",),
        )
    command = build_host_command(host, executable, prompt, workspace)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        return HostExecution(
            command=command,
            exit_code=None,
            stdout="",
            stderr="",
            wall_time_seconds=time.monotonic() - started,
            errors=(f"could not execute {host} executable: {error}",),
        )
    elapsed = time.monotonic() - started
    errors: list[str] = []
    observation: HostObservation | None = None
    tokens: dict[str, int] = {}
    cost: dict[str, float] = {}
    if completed.returncode != 0:
        errors.append(f"{host} exited with exit code {completed.returncode}")
    else:
        parser = _parse_codex if host == "codex" else _parse_claude
        observation, tokens, cost, parse_errors = parser(completed.stdout)
        errors.extend(parse_errors)
    return HostExecution(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        wall_time_seconds=elapsed,
        version=_version(executable),
        observation=observation,
        tokens=tokens,
        cost=cost,
        errors=tuple(errors),
    )
