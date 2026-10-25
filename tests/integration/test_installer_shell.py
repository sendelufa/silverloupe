from __future__ import annotations

import hashlib
import os
import pty
import select
import subprocess
import tarfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "install.sh"


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _tool_path(tmp_path: Path, *, agents: tuple[str, ...] = (), uv: str | None = None) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, target in {
        "mktemp": "/usr/bin/mktemp",
        "mkdir": "/bin/mkdir",
        "rm": "/bin/rm",
        "curl": "/usr/bin/curl",
        "shasum": "/usr/bin/shasum",
        "tar": "/usr/bin/tar",
        "find": "/usr/bin/find",
    }.items():
        (tools / name).symlink_to(target)
    _write_executable(
        tools / "uname",
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-s\" ]; then printf '%s\\n' \"${TEST_UNAME_S:-Darwin}\"; "
        "else printf '%s\\n' \"${TEST_UNAME_M:-arm64}\"; fi\n",
    )
    for name in agents:
        _write_executable(
            tools / name,
            "#!/bin/sh\nprintf 'executed\\n' >> \"$TEST_AGENT_LOG\"\nexit 99\n",
        )
    if uv is not None:
        _write_executable(tools / "uv", uv)
    return tools


def _minimal_release(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "release-source/silverloupe-0.1.0"
    (source / "runtime").mkdir(parents=True)
    _write_executable(
        source / "install.py",
        "#!/usr/bin/env python3\nprint('installer reached')\n",
    )
    (source / "runtime/requirements.lock").write_text("", encoding="utf-8")
    archive = tmp_path / "downloads/silverloupe-0.1.0.tar.gz"
    archive.parent.mkdir(parents=True)
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname=source.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


def _environment(
    tmp_path: Path,
    *,
    agents: tuple[str, ...] = (),
    uv: str | None = None,
) -> dict[str, str]:
    tools = _tool_path(tmp_path, agents=agents, uv=uv)
    return {
        "PATH": str(tools),
        "HOME": str(tmp_path / "home"),
        "TEST_AGENT_LOG": str(tmp_path / "agent.log"),
        "TEST_UNAME_S": "Darwin",
        "TEST_UNAME_M": "arm64",
        "SILVERLOUPE_UV_INSTALLER_URL": "file:///does/not/exist",
    }


def _run_pty(
    command: list[str],
    *,
    env: dict[str, str],
    answers: bytes = b"",
    timeout: float = 10,
) -> tuple[int, str]:
    pid, master = pty.fork()
    if pid == 0:
        os.execve(command[0], command, env)
    output = bytearray()
    if answers:
        os.write(master, answers)
    deadline = time.monotonic() + timeout
    status: int | None = None
    try:
        while time.monotonic() < deadline:
            ready, _writable, _errors = select.select([master], [], [], 0.1)
            if ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    pass
            finished, raw_status = os.waitpid(pid, os.WNOHANG)
            if finished:
                status = os.waitstatus_to_exitcode(raw_status)
                break
        if status is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            pytest.fail(f"PTY command timed out: {output.decode(errors='replace')}")
    finally:
        os.close(master)
    return status, output.decode(errors="replace")


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Linux", "aarch64"), ("Darwin", "x86_64")],
)
def test_shell_rejects_non_darwin_or_non_arm64_before_network(
    tmp_path: Path,
    system: str,
    machine: str,
) -> None:
    env = _environment(tmp_path, agents=("codex",))
    env.update({"TEST_UNAME_S": system, "TEST_UNAME_M": machine})

    completed = subprocess.run(
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True
    )

    assert completed.returncode == 2
    assert "macOS on Apple Silicon" in completed.stderr
    assert not (tmp_path / "agent.log").exists()


def test_shell_stops_before_download_when_no_agent_cli_exists(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    env["SILVERLOUPE_RELEASE_URL"] = "file:///must-not-be-read"

    completed = subprocess.run(
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True
    )

    assert completed.returncode == 2
    assert "Codex or Claude Code" in completed.stderr
    assert "https://" in completed.stderr


@pytest.mark.parametrize("agents", [("codex",), ("codex", "claude")])
def test_shell_reports_one_or_both_agent_paths_without_executing_them(
    tmp_path: Path,
    agents: tuple[str, ...],
) -> None:
    env = _environment(tmp_path, agents=agents)
    env["SILVERLOUPE_RELEASE_URL"] = "file:///missing-release"
    env["SILVERLOUPE_RELEASE_SHA256_URL"] = "file:///missing-checksum"

    completed = subprocess.run(
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True
    )

    assert completed.returncode == 3
    for agent in agents:
        assert str(tmp_path / "tools" / agent) in completed.stdout
    assert not (tmp_path / "agent.log").exists()


def test_shell_prompts_through_dev_tty_when_script_arrives_on_stdin(
    tmp_path: Path,
) -> None:
    archive, checksum = _minimal_release(tmp_path)
    env = _environment(tmp_path, agents=("codex",))
    env.update(
        {
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
        }
    )
    command = ["/bin/sh", "-c", f"/bin/cat {SCRIPT} | /bin/sh"]

    returncode, output = _run_pty(command, env=env, answers=b"n\n")

    assert returncode == 0
    assert "Install uv now?" in output
    assert "Installation cancelled" in output


def test_shell_quotes_release_and_home_paths_with_spaces_and_unicode(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "Релиз с пробелами"
    archive, checksum = _minimal_release(fixture_root)
    uv_log = tmp_path / "uv.log"
    uv = (
        "#!/bin/sh\n"
        "printf '<%s>\\n' \"$@\" >> \"$TEST_UV_LOG\"\n"
        "if [ \"${1:-}\" = python ] && [ \"${2:-}\" = find ]; then exit 0; fi\n"
        "exit 0\n"
    )
    env = _environment(tmp_path, agents=("codex",), uv=uv)
    env.update(
        {
            "HOME": str(tmp_path / "Дом с пробелами"),
            "TEST_UV_LOG": str(uv_log),
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
        }
    )

    returncode, _output = _run_pty(["/bin/sh", str(SCRIPT)], env=env)

    assert returncode == 0
    log = uv_log.read_text(encoding="utf-8")
    assert "<python>\n<find>\n<3.12>" in log
    assert any(line.endswith("/install.py>") for line in log.splitlines())


def test_truncated_download_exits_before_tar_or_uv(tmp_path: Path) -> None:
    archive, checksum = _minimal_release(tmp_path)
    archive.write_bytes(b"truncated")
    checksum.write_text(
        f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
        encoding="utf-8",
    )
    uv_log = tmp_path / "uv.log"
    env = _environment(
        tmp_path,
        agents=("codex",),
        uv="#!/bin/sh\nprintf 'called\\n' >> \"$TEST_UV_LOG\"\n",
    )
    env.update(
        {
            "TEST_UV_LOG": str(uv_log),
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
        }
    )

    completed = subprocess.run(
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True
    )

    assert completed.returncode == 3
    assert "could not be extracted" in completed.stderr
    assert not uv_log.exists()


def test_checksum_mismatch_exits_before_extraction(tmp_path: Path) -> None:
    archive, checksum = _minimal_release(tmp_path)
    checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")
    uv_log = tmp_path / "uv.log"
    env = _environment(
        tmp_path,
        agents=("codex",),
        uv="#!/bin/sh\nprintf 'called\\n' >> \"$TEST_UV_LOG\"\n",
    )
    env.update(
        {
            "TEST_UV_LOG": str(uv_log),
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
        }
    )

    completed = subprocess.run(
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True
    )

    assert completed.returncode == 3
    assert "checksum does not match" in completed.stderr
    assert not uv_log.exists()


def test_missing_uv_requires_consent_and_uses_official_installer_without_sudo(
    tmp_path: Path,
) -> None:
    archive, checksum = _minimal_release(tmp_path)
    uv_log = tmp_path / "uv.log"
    uv_installer = tmp_path / "official-uv-install.sh"
    uv_installer.write_text(
        "#!/bin/sh\n"
        "/bin/mkdir -p \"$HOME/.local/bin\"\n"
        "printf '%s\\n' '#!/bin/sh' "
        "'printf \"<%s>\\\\n\" \"$@\" >> \"$TEST_UV_LOG\"' "
        "'if [ \"${1:-}\" = python ] && [ \"${2:-}\" = find ]; then exit 0; fi' "
        "'exit 0' > \"$HOME/.local/bin/uv\"\n"
        "/bin/chmod +x \"$HOME/.local/bin/uv\"\n",
        encoding="utf-8",
    )
    env = _environment(tmp_path, agents=("codex",))
    env.update(
        {
            "TEST_UV_LOG": str(uv_log),
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
            "SILVERLOUPE_UV_INSTALLER_URL": uv_installer.as_uri(),
        }
    )

    returncode, output = _run_pty(
        ["/bin/sh", str(SCRIPT)], env=env, answers=b"y\n"
    )

    assert returncode == 0
    assert "Install uv now?" in output
    assert "sudo" not in uv_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("missing", ["uv", "python"])
def test_declined_uv_or_python_leaves_no_silverloupe_files(
    tmp_path: Path,
    missing: str,
) -> None:
    archive, checksum = _minimal_release(tmp_path)
    uv = None
    if missing == "python":
        uv = "#!/bin/sh\nexit 1\n"
    env = _environment(tmp_path, agents=("codex",), uv=uv)
    env.update(
        {
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
        }
    )

    returncode, output = _run_pty(
        ["/bin/sh", str(SCRIPT)], env=env, answers=b"n\n"
    )

    assert returncode == 0
    assert "Installation cancelled" in output
    assert not (tmp_path / "home/Library/Application Support/Silverloupe").exists()
    assert not (tmp_path / "home/.agents").exists()


@pytest.mark.parametrize("python_ready", [True, False])
def test_python_312_is_installed_only_when_uv_cannot_find_it(
    tmp_path: Path,
    python_ready: bool,
) -> None:
    archive, checksum = _minimal_release(tmp_path)
    uv_log = tmp_path / "uv.log"
    find_status = 0 if python_ready else 1
    uv = (
        "#!/bin/sh\n"
        "printf '<%s>\\n' \"$@\" >> \"$TEST_UV_LOG\"\n"
        f"if [ \"${{1:-}}\" = python ] && [ \"${{2:-}}\" = find ]; then exit {find_status}; fi\n"
        "exit 0\n"
    )
    env = _environment(tmp_path, agents=("codex",), uv=uv)
    env.update(
        {
            "TEST_UV_LOG": str(uv_log),
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
        }
    )

    answers = b"" if python_ready else b"y\n"
    returncode, _output = _run_pty(
        ["/bin/sh", str(SCRIPT)], env=env, answers=answers
    )

    assert returncode == 0
    log = uv_log.read_text(encoding="utf-8")
    assert ("<python>\n<install>\n<3.12>" in log) is (not python_ready)


def test_verified_archive_launches_install_py_with_dev_tty(tmp_path: Path) -> None:
    archive, checksum = _minimal_release(tmp_path)
    uv_log = tmp_path / "uv.log"
    uv = (
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = python ] && [ \"${2:-}\" = find ]; then exit 0; fi\n"
        "if [ -t 0 ]; then printf 'tty\\n' >> \"$TEST_UV_LOG\"; else exit 7; fi\n"
        "printf '<%s>\\n' \"$@\" >> \"$TEST_UV_LOG\"\n"
        "exit 0\n"
    )
    env = _environment(tmp_path, agents=("codex",), uv=uv)
    env.update(
        {
            "TEST_UV_LOG": str(uv_log),
            "SILVERLOUPE_RELEASE_URL": archive.as_uri(),
            "SILVERLOUPE_RELEASE_SHA256_URL": checksum.as_uri(),
        }
    )

    returncode, _output = _run_pty(["/bin/sh", str(SCRIPT)], env=env)

    assert returncode == 0
    log = uv_log.read_text(encoding="utf-8")
    assert log.startswith("tty\n")
    assert any(line.endswith("/install.py>") for line in log.splitlines())
