from __future__ import annotations

import curses
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest

from silverloupe.installer.presentation import (
    CursesWizardIO,
    InstallerCancelled,
    LineWizardIO,
    human_result_rows,
)


class _Screen:
    def __init__(self, keys: list[int]) -> None:
        self._keys = iter(keys)
        self.lines: list[str] = []

    def clear(self) -> None:
        self.lines.clear()

    def addstr(self, _row: int, _column: int, value: str, *_attributes: int) -> None:
        self.lines.append(value)

    def refresh(self) -> None:
        pass

    def keypad(self, _enabled: bool) -> None:
        pass

    def getch(self) -> int:
        return next(self._keys)


def _wrapper(screen: _Screen) -> Callable[..., Any]:
    def run(function: Callable[..., Any], *args: object) -> Any:
        return function(screen, *args)

    return run


def test_line_ui_renders_one_decision_at_a_time() -> None:
    output = StringIO()
    io = LineWizardIO(input_stream=StringIO("2\n"), output_stream=output)

    selected = io.choose_one(
        "Choose agent",
        "Only detected agents are shown.",
        (("codex", "Codex"), ("claude-code", "Claude Code")),
        default="codex",
    )

    assert selected == "claude-code"
    assert output.getvalue().splitlines() == [
        "Choose agent",
        "Only detected agents are shown.",
        "  1. Codex [recommended]",
        "  2. Claude Code",
        "Select [1]: ",
    ]


def test_curses_ui_maps_arrows_space_enter_escape_without_snapshotting_ansi() -> None:
    screen = _Screen([curses.KEY_DOWN, ord(" "), 10])
    io = CursesWizardIO(wrapper=_wrapper(screen))

    selected = io.choose_many(
        "Choose agents",
        "Space toggles a host.",
        (("codex", "Codex"), ("claude-code", "Claude Code")),
        defaults=("codex",),
    )

    assert selected == ("codex", "claude-code")
    assert any("Claude Code" in line for line in screen.lines)


def test_curses_failure_falls_back_to_numbered_line_ui() -> None:
    fallback = LineWizardIO(input_stream=StringIO("1\n"), output_stream=StringIO())

    def broken_wrapper(_function: Callable[..., Any], *_args: object) -> Any:
        raise curses.error("no terminal")

    io = CursesWizardIO(wrapper=broken_wrapper, fallback=fallback)

    assert (
        io.choose_one(
            "Choose editor",
            "Detected applications",
            (("report-only", "Reports only"),),
            default="report-only",
        )
        == "report-only"
    )


@pytest.mark.parametrize("key", [27, 3])
def test_ctrl_c_and_escape_restore_terminal_and_cancel(key: int) -> None:
    io = CursesWizardIO(wrapper=_wrapper(_Screen([key])))

    with pytest.raises(InstallerCancelled):
        io.confirm("Install", "Ready to install", default=True)


def test_curses_enter_accepts_displayed_destructive_default() -> None:
    io = CursesWizardIO(wrapper=_wrapper(_Screen([10])))

    assert io.confirm("Uninstall", "Remove Silverloupe?", default=False) is False


def test_human_result_separates_ready_report_only_and_unavailable() -> None:
    rows = human_result_rows(
        {
            "status": "degraded",
            "hosts": {"codex": {"status": "ready"}},
            "editors": {
                "lightcraft": {
                    "status": "unavailable",
                    "effective_mode": "report-only",
                    "reason": "native integration unavailable",
                }
            },
            "raw": {"status": "unavailable", "reason": "decoder missing"},
        }
    )

    assert ("Codex", "ready", "") in rows
    assert ("LightCraft", "report-only", "native integration unavailable") in rows
    assert ("RAW", "unavailable", "decoder missing") in rows
