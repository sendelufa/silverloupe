# Friendly One-Command Installer Design

## Intent

Silverloupe should be installable by a photographer who is comfortable pasting one
command into Terminal but does not want to download, unpack, or inspect a release
archive manually. The installer should ask only questions whose answers materially
change the installation. All remaining choices use visible, conservative defaults.

After a later publication step, the intended user-facing entry point is:

```bash
curl -fsSL https://github.com/sendelufa/silverloupe/releases/latest/download/install.sh | sh
```

The future hosted script is a small POSIX shell bootstrap. It downloads the
production release automatically and then launches a guided terminal interface.
This change builds and locally qualifies that bootstrap and TUI, but deliberately
does not add GitHub Actions, publish release assets, create a tag, or create a
GitHub Release. Publication is a separate follow-up.

## Goals

- A bootstrap designed to become one copy-and-paste command from a normal macOS
  Terminal once it is published.
- A guided, keyboard-driven TUI with one decision per screen.
- Support macOS on Apple Silicon only in the first release.
- Detect Codex and Claude Code before changing the machine.
- Stop without installing Silverloupe, `uv`, or Python when neither agent CLI exists.
- Never install or modify Codex or Claude Code.
- Install `uv` and an isolated Python 3.12 automatically after explicit consent.
- Detect supported photo editors, but allow installation without one.
- Default to report/selection-only behavior when no verified writer is available.
- Show a concise review before making persistent changes.
- Keep the existing non-interactive flags for automation and testing.
- Make repeated invocation useful for installation, update, reconfiguration, and
  uninstall entry points.

## Non-goals

- Linux, Windows, or Intel macOS support.
- Installing Codex, Claude Code, or photo editors.
- A graphical `.app`, `.pkg`, Homebrew formula, or App Store distribution.
- Requiring users to choose cache sizes, worker counts, model profiles, install
  paths, or dependency groups during the normal flow.
- Claiming native editor integration is ready before its runtime capability probe
  succeeds.
- Adding release signing in this change. The bootstrap uses GitHub HTTPS, release
  checksums, and the existing per-file manifest verification; signing remains a
  separate release-hardening step.
- Publishing `install.sh`, a production archive, or checksums to GitHub, and adding
  a tag-triggered release workflow.

## User Journey

### 1. Bootstrap preflight

The shell bootstrap performs only checks that work on stock macOS:

1. Require an interactive terminal and access prompts through `/dev/tty`, because
   the script itself arrives through stdin.
2. Require `Darwin` and `arm64`; unsupported systems get a short explanation and
   no changes.
3. Locate `codex` and `claude` with `command -v` and record executable paths.
4. If neither exists, explain that Silverloupe is operated through agent skills,
   show official installation links, and exit before downloading or installing
   dependencies.
5. Resolve the latest stable GitHub release, download its production archive and
   published SHA-256 file into a temporary directory, and verify the archive before
   extraction.
6. If `uv` is absent, explain what it is, where the official installer comes from,
   and ask permission to install it without `sudo`.
7. Ask permission before `uv` installs its managed Python 3.12 when that interpreter
   is not already available.
8. Run the verified Python installer with `/dev/tty` attached.

Cancellation or failure removes temporary files and leaves existing Silverloupe
installations active. Dependency installation is never attempted before at least
one agent CLI has been found.

### 2. Guided TUI

The selected layout is a step-by-step wizard. Each screen has a clear title, a
short explanation, one primary decision, a visible recommended choice, and a footer
with keyboard help. Arrow keys move, Space toggles multi-select choices, Enter
continues, and Escape backs out or cancels. A plain numbered-prompt fallback is
available when curses cannot initialize.

The wizard asks only these questions:

1. **Agent host**, and only when both Codex and Claude Code are detected. If exactly
   one exists, it is selected automatically and merely reported.
2. **Photo editor**, and only when one or more supported editors are detected. The
   user may explicitly choose “No editor — reports only.” With no detected editor,
   report-only is selected automatically.
3. **Permission to install missing `uv` or Python**, handled by bootstrap preflight.
4. **Final confirmation** after the review screen.

The normal flow does not ask about scope, destination, model mode, RAW, cache quota,
or workers. Defaults are user scope, the platform-standard Silverloupe data
directory, host vision, 5 GiB cache quota, and one worker. Optional RAW support is
off initially and can be offered later when a workflow actually encounters RAW
files.

### 3. Detection and safe capability behavior

Agent detection uses executable presence and shows the resolved path. It does not
run login, update, or install commands for the agent.

Editor detection checks standard macOS application locations and Spotlight when
available for Lightroom Classic, Lightroom, Photoshop/Camera Raw, and LightCraft.
Detection means only that an application appears installed. It does not prove a
metadata integration. The review screen distinguishes:

- detected application;
- requested integration;
- effective capability after the production probe.

If no compatible editor exists, installation continues with selection, comparison,
and static reports enabled and metadata delivery disabled. If a selected integration
cannot be verified, installation succeeds in report-only mode and explains how to
rerun `doctor` later.

### 4. Review, installation, and result

The review screen shows the selected agent hosts, editor or report-only mode,
Silverloupe version, install destination, skill destinations, and any downloads
still required. No Silverloupe files persist until the user confirms this screen.
If the user separately approved bootstrap installation of `uv` or managed Python
before the TUI launched, those general-purpose tools remain installed after a later
Silverloupe cancellation; the bootstrap states this before requesting consent.

Installation delegates to the existing verified lifecycle and retains its ownership
manifest, versioned runtime, transactional activation, rollback, safe update, and
safe uninstall behavior. On success the wizard runs `silverloupe doctor` and renders
the result as short human-readable rows rather than raw JSON. It ends with:

- what was installed;
- where the six skills were registered;
- which features are ready or report-only;
- a first command or example prompt to use inside the selected agent;
- the launcher path for `doctor`, `configure`, `update`, and `uninstall`.

On failure it shows the failed stage, preserves the previous active installation,
and provides a log path with secrets and environment values excluded.

## Architecture

### Shell bootstrap

Add a release-oriented `packaging/install.sh` that can later be copied to release
assets.
It uses only stock macOS tools (`sh`, `curl`, `uname`, `mktemp`, `shasum`, and
`tar`) until `uv` is installed. All network URLs are HTTPS and derived from the
configured release source. Downloads have explicit failure handling and temporary
directories are removed by a trap. Tests use a local HTTP fixture or explicit test
override so they do not depend on a published GitHub Release.

The bootstrap is designed for a future release channel containing:

- `install.sh`;
- `silverloupe-VERSION.tar.gz`;
- `silverloupe-VERSION.tar.gz.sha256`.

The bootstrap verifies the archive SHA-256 before extraction. `install.py` then
performs the existing exact ownership and per-file checksum verification before
importing bundled runtime code.

### Python installer UI

Refactor `packaging/install.py` so argument parsing, environment discovery,
configuration creation, installation execution, and presentation are separate
functions. The interactive path uses a small standard-library curses UI, keeping
the bootstrap dependency surface unchanged. A line-oriented fallback implements
the same state machine for inaccessible terminals and tests.

The existing CLI flags remain authoritative: when all required flags are supplied,
the command stays non-interactive and machine-readable. A new `--non-interactive`
flag makes missing required values an error instead of opening the wizard.

Editor choice maps to a fixed conservative policy rather than another question.
LightCraft requests `native` mode, but the existing capability gate falls back to
report-only unless the selected agent can verify the MCP route. Adobe applications
request report-only in this release because their real application round trips are
not release-qualified. Selecting Adobe records the intended editor for later
reconfiguration without implying that metadata delivery is ready.

### Discovery model

Introduce immutable discovery records for platform, agent executables, editors,
`uv`, Python, disk space, and existing installation state. Discovery itself is
read-only and injectable in tests. The wizard converts a discovery result plus user
choices into the existing strict `InstallConfig`; the lifecycle remains the only
component allowed to mutate installation state.

### Repeat invocation

When the default installation is already present, the TUI offers three top-level
actions: update to the resolved release, reconfigure hosts/editor, or uninstall.
Destructive uninstall retains the existing owned-file protections and requires a
separate confirmation. A first installation never displays this management menu.

## Error and Cancellation Semantics

- Escape before final confirmation exits with no Silverloupe changes.
- Missing agents is a prerequisite failure, not a degraded installation.
- Missing editors is non-blocking and selects report-only.
- Network, checksum, archive, dependency, and disk failures identify the failed
  stage and do not switch the active version.
- Declining `uv` or Python installation exits cleanly with a copyable retry command.
- Cancelling Silverloupe after separately approving `uv` or Python leaves those
  tools installed and says so explicitly; it does not attempt a risky shared-tool
  uninstall.
- Ctrl-C restores the terminal before exiting.
- No flow requests `sudo` or writes outside the user account.

## Testing and Acceptance

Automated tests cover:

- stock-shell preflight for supported/unsupported platform and zero/one/two agents;
- the `/dev/tty` interaction required by `curl | sh`;
- download failure and SHA-256 mismatch before extraction;
- permission declined for `uv` and Python;
- discovery of zero/one/multiple editors;
- minimal-question behavior for every discovery combination;
- TUI state transitions, back/cancel behavior, and line-mode fallback;
- translation to `InstallConfig` and retention of existing non-interactive flags;
- missing-agent hard stop before any mutation;
- missing-editor successful report-only install;
- existing-install update/configure/uninstall menu;
- full clean-home smoke using the local bootstrap fixture through successful
  `doctor`;
- regression of the production archive, ownership, rollback, and uninstall suites.

Manual qualification uses a clean Apple Silicon macOS account with Codex
only, Claude Code only, both agents, and no editor. The acceptance bar is that a
new user can run the locally staged bootstrap, answer at most the genuinely
applicable host/editor questions plus confirmations, and reach a successful
human-readable doctor result without manually handling an archive or Python
environment. Making the exact public `curl` command live is explicitly outside
this change.

## Builder Decisions

The builder may choose exact colors, spacing, Unicode/ASCII fallback glyphs,
function names, and internal module boundaries. Tests should drive behavior rather
than terminal escape-sequence snapshots. The implementation should prefer the
existing installer lifecycle over duplicating installation logic.
