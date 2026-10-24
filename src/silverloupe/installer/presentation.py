"""Terminal presentation adapters for the guided installer."""

from __future__ import annotations

import curses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO
import sys


Option = tuple[str, str]
ResultRow = tuple[str, str, str]


class InstallerCancelled(Exception):
    """Raised when the user cancels the terminal wizard."""


class WizardIO(Protocol):
    def choose_one(
        self,
        title: str,
        explanation: str,
        options: Sequence[Option],
        *,
        default: str,
    ) -> str: ...

    def choose_many(
        self,
        title: str,
        explanation: str,
        options: Sequence[Option],
        *,
        defaults: Sequence[str],
    ) -> tuple[str, ...]: ...

    def confirm(self, title: str, explanation: str, *, default: bool) -> bool: ...

    def show_status(self, title: str, rows: Sequence[ResultRow]) -> None: ...

    def show_result(self, title: str, rows: Sequence[ResultRow]) -> None: ...


@dataclass
class LineWizardIO:
    input_stream: TextIO = sys.stdin
    output_stream: TextIO = sys.stdout

    def _write_options(
        self,
        title: str,
        explanation: str,
        options: Sequence[Option],
        default: str,
    ) -> None:
        self.output_stream.write(f"{title}\n{explanation}\n")
        for index, (value, label) in enumerate(options, start=1):
            suffix = " [recommended]" if value == default else ""
            self.output_stream.write(f"  {index}. {label}{suffix}\n")

    def choose_one(
        self,
        title: str,
        explanation: str,
        options: Sequence[Option],
        *,
        default: str,
    ) -> str:
        if not options:
            raise ValueError("choose_one requires at least one option")
        values = [value for value, _label in options]
        default_index = values.index(default) + 1
        while True:
            self._write_options(title, explanation, options, default)
            self.output_stream.write(f"Select [{default_index}]: \n")
            self.output_stream.flush()
            answer = self.input_stream.readline()
            if answer == "":
                raise InstallerCancelled()
            answer = answer.strip()
            if not answer:
                return default
            if answer.casefold() in {"q", "quit", "cancel"}:
                raise InstallerCancelled()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return values[int(answer) - 1]
            self.output_stream.write("Choose one of the listed numbers.\n")

    def choose_many(
        self,
        title: str,
        explanation: str,
        options: Sequence[Option],
        *,
        defaults: Sequence[str],
    ) -> tuple[str, ...]:
        default = ",".join(
            str(index)
            for index, (value, _label) in enumerate(options, start=1)
            if value in defaults
        )
        self.output_stream.write(f"{title}\n{explanation}\n")
        for index, (value, label) in enumerate(options, start=1):
            suffix = " [recommended]" if value in defaults else ""
            self.output_stream.write(f"  {index}. {label}{suffix}\n")
        while True:
            self.output_stream.write(f"Select comma-separated values [{default}]: \n")
            self.output_stream.flush()
            answer = self.input_stream.readline()
            if answer == "":
                raise InstallerCancelled()
            answer = answer.strip() or default
            if answer.casefold() in {"q", "quit", "cancel"}:
                raise InstallerCancelled()
            parts = [part.strip() for part in answer.split(",")]
            if parts and all(part.isdigit() and 1 <= int(part) <= len(options) for part in parts):
                indexes = sorted({int(part) - 1 for part in parts})
                return tuple(options[index][0] for index in indexes)
            self.output_stream.write("Choose one or more listed numbers.\n")

    def confirm(self, title: str, explanation: str, *, default: bool) -> bool:
        value = self.choose_one(
            title,
            explanation,
            (("yes", "Yes"), ("no", "No")),
            default="yes" if default else "no",
        )
        return value == "yes"

    def show_status(self, title: str, rows: Sequence[ResultRow]) -> None:
        self._show_rows(title, rows)

    def show_result(self, title: str, rows: Sequence[ResultRow]) -> None:
        self._show_rows(title, rows)

    def _show_rows(self, title: str, rows: Sequence[ResultRow]) -> None:
        self.output_stream.write(f"{title}\n")
        for label, status, detail in rows:
            suffix = f" — {detail}" if detail else ""
            self.output_stream.write(f"  {label}: {status}{suffix}\n")
        self.output_stream.flush()


class CursesWizardIO:
    def __init__(
        self,
        *,
        wrapper: Callable[..., Any] = curses.wrapper,
        fallback: WizardIO | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._fallback = fallback or LineWizardIO()

    def _safe_addstr(self, screen: Any, row: int, column: int, value: str, *attrs: int) -> None:
        try:
            screen.addstr(row, column, value, *attrs)
        except (UnicodeError, curses.error):
            screen.addstr(row, column, value.encode("ascii", "replace").decode("ascii"))

    def _menu(
        self,
        screen: Any,
        title: str,
        explanation: str,
        options: Sequence[Option],
        defaults: Sequence[str],
        multi: bool,
    ) -> tuple[str, ...]:
        if not options:
            raise ValueError("menu requires at least one option")
        screen.keypad(True)
        index = 0
        selected = {
            option_index
            for option_index, (value, _label) in enumerate(options)
            if value in defaults
        }
        while True:
            screen.clear()
            self._safe_addstr(screen, 0, 0, f"Silverloupe — {title}")
            self._safe_addstr(screen, 2, 0, explanation)
            for option_index, (_value, label) in enumerate(options):
                cursor = ">" if option_index == index else " "
                mark = "[x]" if option_index in selected else "[ ]"
                self._safe_addstr(screen, 4 + option_index, 0, f"{cursor} {mark} {label}")
            footer = "Arrows move · Space selects · Enter continues · Esc cancels"
            self._safe_addstr(screen, 6 + len(options), 0, footer)
            screen.refresh()
            key = screen.getch()
            if key in {27, 3}:
                raise InstallerCancelled()
            if key in {curses.KEY_UP, ord("k")}:
                index = (index - 1) % len(options)
            elif key in {curses.KEY_DOWN, ord("j")}:
                index = (index + 1) % len(options)
            elif key == ord(" "):
                if multi:
                    if index in selected:
                        selected.remove(index)
                    else:
                        selected.add(index)
                else:
                    selected = {index}
            elif key in {10, 13, curses.KEY_ENTER}:
                if multi:
                    if selected:
                        return tuple(options[value][0] for value in sorted(selected))
                else:
                    return (options[index][0],)

    def _run_or_fallback(
        self,
        fallback: Callable[[], Any],
        *args: object,
    ) -> Any:
        try:
            return self._wrapper(self._menu, *args)
        except curses.error:
            return fallback()

    def choose_one(
        self,
        title: str,
        explanation: str,
        options: Sequence[Option],
        *,
        default: str,
    ) -> str:
        result = self._run_or_fallback(
            lambda: self._fallback.choose_one(
                title, explanation, options, default=default
            ),
            title,
            explanation,
            options,
            (default,),
            False,
        )
        return result[0] if isinstance(result, tuple) else result

    def choose_many(
        self,
        title: str,
        explanation: str,
        options: Sequence[Option],
        *,
        defaults: Sequence[str],
    ) -> tuple[str, ...]:
        result = self._run_or_fallback(
            lambda: self._fallback.choose_many(
                title, explanation, options, defaults=defaults
            ),
            title,
            explanation,
            options,
            defaults,
            True,
        )
        return tuple(result)

    def confirm(self, title: str, explanation: str, *, default: bool) -> bool:
        value = self.choose_one(
            title,
            explanation,
            (("yes", "Yes"), ("no", "No")),
            default="yes" if default else "no",
        )
        return value == "yes"

    def show_status(self, title: str, rows: Sequence[ResultRow]) -> None:
        self._fallback.show_status(title, rows)

    def show_result(self, title: str, rows: Sequence[ResultRow]) -> None:
        self._fallback.show_result(title, rows)


def _label(value: str) -> str:
    return {
        "codex": "Codex",
        "claude-code": "Claude Code",
        "lightroom-classic": "Lightroom Classic",
        "lightroom-cloud": "Lightroom",
        "photoshop-camera-raw": "Photoshop / Camera Raw",
        "lightcraft": "LightCraft",
        "raw": "RAW",
    }.get(value, value.replace("-", " ").title())


def human_result_rows(capabilities: Mapping[str, object]) -> list[ResultRow]:
    """Flatten capability JSON into short, honest rows for a person."""

    rows: list[ResultRow] = []
    for section in ("hosts", "editors"):
        raw_section = capabilities.get(section, {})
        if not isinstance(raw_section, Mapping):
            continue
        for name, raw_value in raw_section.items():
            details = raw_value if isinstance(raw_value, Mapping) else {}
            status = str(details.get("status", "unavailable"))
            effective = details.get("effective_mode")
            if effective == "report-only":
                status = "report-only"
            rows.append(
                (
                    _label(str(name)),
                    status,
                    str(details.get("reason", "")),
                )
            )
    raw = capabilities.get("raw")
    if isinstance(raw, Mapping):
        rows.append(
            ("RAW", str(raw.get("status", "unavailable")), str(raw.get("reason", "")))
        )
    return rows
