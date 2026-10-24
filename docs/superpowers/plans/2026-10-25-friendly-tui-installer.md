# Friendly TUI Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and locally qualify a one-command-ready macOS Apple Silicon bootstrap that discovers agent CLIs, installs `uv`/Python with consent, and guides users through a minimal terminal installer.

**Architecture:** A stock-POSIX-shell bootstrap performs immutable platform/agent/download checks before invoking Python. Verified bundled runtime code owns environment discovery, a pure wizard state machine, curses/line presentation, and translation into the existing transactional installer lifecycle. Release asset generation stays local; GitHub publication is explicitly deferred.

**Tech Stack:** POSIX `sh`, stock macOS `curl`/`shasum`/`tar`, Python 3.12 standard library (`dataclasses`, `curses`, `pathlib`), Pydantic `InstallConfig`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-25-friendly-tui-installer-design.md`

## Global Constraints

- Support only macOS Apple Silicon: `Darwin` plus `arm64`/`aarch64`.
- Require at least one detected `codex` or `claude` executable before any download or dependency installation.
- Never install or modify Codex, Claude Code, or photo editors.
- Install `uv` and uv-managed Python 3.12 only after separate explicit consent and never with `sudo`.
- Ask only host selection when both agents exist, editor selection when editors exist, dependency consent when needed, and final confirmation.
- Use user scope, `~/Library/Application Support/Silverloupe`, host vision, 5 GiB cache, one worker, and RAW disabled without asking.
- LightCraft requests `native` with capability fallback; all Adobe choices request `report-only`; no editor means `report-only`.
- Keep existing fully specified CLI invocations non-interactive and machine-readable; `--non-interactive` rejects missing required values.
- Preserve exact archive/manifest verification, ownership, transactional activation, rollback, update, and uninstall safety.
- Do not add publishing workflows, tags, GitHub Releases, public assets, or release signing.

## Review Focus

- `curl | sh` has script bytes on stdin: every user prompt and Python wizard launch must use `/dev/tty`; Task 3 adds a pseudo-terminal test.
- Home/install paths may contain spaces and non-ASCII characters: Task 2 installs into such a default root and asserts exact paths survive configuration and launcher creation.
- A download can truncate or its checksum can disagree: Task 3 proves neither archive extraction nor dependency installation starts.
- An agent executable can disappear after shell preflight: Task 2 re-discovers agents immediately before final install and aborts before lifecycle mutation.
- Existing installations can contain modified/foreign owned targets: Task 4 exercises update/uninstall through the wizard and proves existing lifecycle preservation reports remain intact.

---

### Task 1: Immutable environment discovery and minimal-question policy

**Files:**
- Create: `src/silverloupe/installer/discovery.py`
- Create: `src/silverloupe/installer/wizard.py`
- Test: `tests/installer/test_installer_wizard.py`

**Interfaces:**
- Consumes: existing `silverloupe.contracts.InstallConfig` and `installer.lifecycle.load_installed_config(Path)`.
- Produces: frozen `AgentInstallDiscovery`, `EditorInstallDiscovery`, and `InstallDiscovery`; `default_install_root(home: Path) -> Path`; `discover_install_environment(install_root: Path) -> InstallDiscovery`; `WizardAction`, `WizardSelection`; `questions_for(discovery: InstallDiscovery) -> tuple[str, ...]`; `build_install_config(discovery: InstallDiscovery, selection: WizardSelection, version: str) -> InstallConfig`.

- [ ] **Step 1: Write failing discovery tests**

Add tests named:

- `test_discovery_rejects_non_macos_and_non_arm64`;
- `test_discovery_records_resolved_codex_and_claude_paths`;
- `test_discovery_finds_supported_apps_without_claiming_integration`;
- `test_default_root_is_user_library_application_support_with_spaces`;
- `test_discovery_marks_existing_install_only_for_valid_config`.

Inject platform values, executable lookup, application roots, home, and disk usage so tests never inspect the developer machine. Assert editor discovery is evidence only and carries no “ready” integration status.

- [ ] **Step 2: Run discovery tests to verify they fail**

Run: `uv run --locked pytest tests/installer/test_installer_wizard.py -k discovery -q`

Expected: FAIL because `silverloupe.installer.discovery` does not exist.

- [ ] **Step 3: Implement immutable discovery records and probes**

In `discovery.py`, define frozen dataclasses with these stable fields:

- `AgentInstallDiscovery(host: Literal["codex", "claude-code"], executable: Path)`;
- `EditorInstallDiscovery(editor: InstallEditor, application_path: Path)`;
- `InstallDiscovery(system: str, machine: str, agents: tuple[...], editors: tuple[...], uv_path: Path | None, python_312_available: bool, free_bytes: int, install_root: Path, existing_config: InstallConfig | None)`.

`discover_install_environment` must remain read-only, resolve executables, search only standard macOS application roots plus an injectable Spotlight callback, and treat malformed existing state as absent with a diagnostic rather than as an installable target.

- [ ] **Step 4: Run discovery tests to verify they pass**

Run: `uv run --locked pytest tests/installer/test_installer_wizard.py -k discovery -q`

Expected: all discovery tests PASS.

- [ ] **Step 5: Write failing minimal-question and configuration tests**

Add tests named:

- `test_zero_agents_is_a_hard_stop_with_no_questions`;
- `test_one_agent_and_no_editor_needs_only_final_confirmation`;
- `test_two_agents_add_exactly_one_host_question`;
- `test_detected_editors_add_exactly_one_editor_question`;
- `test_lightcraft_maps_to_native_and_adobe_maps_to_report_only`;
- `test_config_uses_fixed_user_defaults_and_raw_is_off`;
- `test_invalid_or_disappeared_selected_agent_is_rejected`.

Assert the exact fixed defaults from Global Constraints and that editor choice never creates an unverified file/native claim.

- [ ] **Step 6: Run policy tests to verify they fail**

Run: `uv run --locked pytest tests/installer/test_installer_wizard.py -k 'questions or maps or defaults or disappeared or hard_stop' -q`

Expected: FAIL because wizard policy interfaces are missing.

- [ ] **Step 7: Implement the pure wizard policy**

In `wizard.py`, define `WizardAction = Literal["install", "update", "reconfigure", "uninstall", "cancel"]` and frozen `WizardSelection(action, hosts, editor, confirmed)`. Implement `questions_for` and `build_install_config` without terminal I/O or filesystem mutation. Raise a typed `InstallerPrerequisiteError` for unsupported platform, no agents, stale host selection, or insufficient disk.

- [ ] **Step 8: Run Task 1 tests and full installer regression**

Run: `uv run --locked pytest tests/installer/test_installer_wizard.py tests/integration/test_installer.py -q`

Expected: new tests and all existing installer tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/silverloupe/installer/discovery.py src/silverloupe/installer/wizard.py tests/installer/test_installer_wizard.py
git commit -m "feat: discover friendly installer choices"
```

### Task 2: Guided curses wizard and verified installer entry point

**Files:**
- Create: `src/silverloupe/installer/presentation.py`
- Modify: `src/silverloupe/installer/wizard.py`
- Modify: `packaging/install.py`
- Test: `tests/installer/test_installer_presentation.py`
- Modify: `tests/integration/test_installer.py`

**Interfaces:**
- Consumes: Task 1 discovery and wizard-policy interfaces; existing `install_release`, `update_release`, `uninstall_release`, and `probe_install`.
- Produces: `WizardIO` protocol with `choose_one`, `choose_many`, `confirm`, `show_status`, and `show_result`; `LineWizardIO`; `CursesWizardIO`; `run_installer_wizard(release_root: Path, manifest: Mapping[str, object], discovery: InstallDiscovery, io: WizardIO) -> dict[str, object]`; refactored `packaging.install.main(argv, *, discovery=None, io=None) -> int`.

- [ ] **Step 1: Write failing presentation adapter tests**

Add tests named:

- `test_line_ui_renders_one_decision_at_a_time`;
- `test_curses_ui_maps_arrows_space_enter_escape_without_snapshotting_ansi`;
- `test_curses_failure_falls_back_to_numbered_line_ui`;
- `test_ctrl_c_and_escape_restore_terminal_and_cancel`;
- `test_human_result_separates_ready_report_only_and_unavailable`.

Test logical key events and captured semantic rows, not exact escape sequences, colors, or spacing.

- [ ] **Step 2: Run presentation tests to verify they fail**

Run: `uv run --locked pytest tests/installer/test_installer_presentation.py -q`

Expected: FAIL because presentation adapters do not exist.

- [ ] **Step 3: Implement the presentation boundary**

Create `presentation.py` with the `WizardIO` protocol and both adapters. `CursesWizardIO` owns terminal setup only inside `curses.wrapper`; all decision logic remains in `wizard.py`. Use ASCII fallbacks when Unicode rendering fails. Line mode reads/writes injected streams so tests remain deterministic.

- [ ] **Step 4: Run presentation tests to verify they pass**

Run: `uv run --locked pytest tests/installer/test_installer_presentation.py -q`

Expected: all presentation tests PASS.

- [ ] **Step 5: Write failing end-to-end wizard tests around `install.py`**

Extend integration coverage with:

- `test_interactive_installer_stops_before_mutation_when_no_agent_exists`;
- `test_single_agent_no_editor_installs_report_only_without_extra_questions`;
- `test_two_agents_and_editors_use_selected_values`;
- `test_agent_disappearing_before_confirmation_aborts_before_install`;
- `test_default_install_root_with_spaces_and_unicode_installs_successfully`;
- `test_non_interactive_requires_all_values_and_preserves_json_stdout`;
- `test_existing_install_offers_update_reconfigure_and_uninstall`;
- `test_success_runs_doctor_and_shows_first_agent_prompt`.

Use a scripted `WizardIO` fake and injected discovery. Assert no lifecycle call occurs before confirmation and that re-discovery catches the vanished executable.

- [ ] **Step 6: Run integration tests to verify they fail**

Run: `uv run --locked pytest tests/integration/test_installer.py -k 'interactive or single_agent or two_agents or disappearing or default_install or non_interactive or existing_install or first_agent' -q`

Expected: FAIL because `install.py` still uses raw `input()` and has no wizard orchestration.

- [ ] **Step 7: Refactor `install.py` and implement wizard orchestration**

Keep `_verify_release_root` as the first operation that can import bundled runtime code. Split parsing, strict non-interactive config creation, discovery, wizard orchestration, lifecycle execution, and result presentation. Fully supplied existing flags retain JSON output; `--non-interactive` plus missing `--host`, `--editor`, or `--install-root` exits with code 2 and never prompts.

For an existing installation, route update/reconfigure/uninstall through the existing lifecycle. Immediately before confirmed mutation, call discovery again and validate selected agent paths still resolve. After success, run the installed launcher’s `doctor` command and convert its JSON to semantic result rows.

- [ ] **Step 8: Run Task 2 tests and production package regression**

Run: `uv run --locked pytest tests/installer/test_installer_presentation.py tests/installer/test_installer_wizard.py tests/integration/test_installer.py tests/contracts/test_production_package.py -q`

Expected: all selected tests PASS.

- [ ] **Step 9: Commit**

```bash
git add src/silverloupe/installer/presentation.py src/silverloupe/installer/wizard.py packaging/install.py tests/installer/test_installer_presentation.py tests/integration/test_installer.py
git commit -m "feat: add guided terminal installer"
```

### Task 3: Stock-shell bootstrap with consent and archive verification

**Files:**
- Create: `packaging/install.sh`
- Create: `tests/integration/test_installer_shell.py`

**Interfaces:**
- Consumes: the production archive layout and Task 2 `install.py` interactive entry point.
- Produces: POSIX `packaging/install.sh`; configurable `SILVERLOUPE_RELEASE_URL` and `SILVERLOUPE_RELEASE_SHA256_URL` inputs for local qualification; exit codes `0` success/cancel, `2` unsupported or prerequisite failure, `3` download/integrity/dependency failure.

- [ ] **Step 1: Write failing shell-preflight tests**

Create subprocess/PTY tests named:

- `test_shell_rejects_non_darwin_or_non_arm64_before_network`;
- `test_shell_stops_before_download_when_no_agent_cli_exists`;
- `test_shell_reports_one_or_both_agent_paths_without_executing_them`;
- `test_shell_prompts_through_dev_tty_when_script_arrives_on_stdin`;
- `test_shell_quotes_release_and_home_paths_with_spaces_and_unicode`.

Use a temporary fake `PATH` containing only allowlisted stock-command shims. The PTY test must invoke the script as `sh` reading script bytes from stdin, matching `curl | sh` semantics.

- [ ] **Step 2: Run shell-preflight tests to verify they fail**

Run: `uv run --locked pytest tests/integration/test_installer_shell.py -k 'rejects or stops or reports or dev_tty or quotes' -q`

Expected: FAIL because `packaging/install.sh` does not exist.

- [ ] **Step 3: Implement platform, agent, TTY, and cleanup preflight**

Use `set -eu`, a `mktemp -d` directory, and an EXIT/HUP/INT/TERM trap. Read answers with `read ... < /dev/tty` without replacing stdin. Perform platform and agent gates before `curl`, the uv installer, or any write outside the temporary directory. Never evaluate data returned by discovery commands.

- [ ] **Step 4: Run shell-preflight tests to verify they pass**

Run: `uv run --locked pytest tests/integration/test_installer_shell.py -k 'rejects or stops or reports or dev_tty or quotes' -q`

Expected: selected tests PASS.

- [ ] **Step 5: Write failing download/dependency tests**

Add tests named:

- `test_truncated_download_exits_before_tar_or_uv`;
- `test_checksum_mismatch_exits_before_extraction`;
- `test_missing_uv_requires_consent_and_uses_official_installer_without_sudo`;
- `test_declined_uv_or_python_leaves_no_silverloupe_files`;
- `test_python_312_is_installed_only_when_uv_cannot_find_it`;
- `test_verified_archive_launches_install_py_with_dev_tty`.

Serve fixtures from a temporary local HTTP server. Fake `uv` records arguments and delegates the final verified `install.py` execution to the test interpreter; assert command logs contain neither `sudo` nor agent-install commands.

- [ ] **Step 6: Run download/dependency tests to verify they fail**

Run: `uv run --locked pytest tests/integration/test_installer_shell.py -k 'download or checksum or uv or python_312 or verified_archive' -q`

Expected: FAIL because download, verification, and dependency flow is incomplete.

- [ ] **Step 7: Implement verified download and dependency consent**

Default future URLs may point at GitHub Releases, but local/test URLs come only from `SILVERLOUPE_RELEASE_URL` and `SILVERLOUPE_RELEASE_SHA256_URL`. Require a single `<hex>  <filename>` checksum record matching the downloaded basename, verify with `shasum -a 256 -c`, extract only after success, then locate the one release root containing `install.py`.

If `uv` is missing, explain and obtain consent before executing the official `https://astral.sh/uv/install.sh` installer; rediscover `uv` afterward. Use `uv python find 3.12` before requesting permission for `uv python install 3.12`. Launch `install.py` with `/dev/tty` as stdin and preserve its exit status.

- [ ] **Step 8: Run all shell tests**

Run: `uv run --locked pytest tests/integration/test_installer_shell.py -q`

Expected: all shell tests PASS without external network access.

- [ ] **Step 9: Commit**

```bash
git add packaging/install.sh tests/integration/test_installer_shell.py
git commit -m "feat: bootstrap terminal installer on macOS"
```

### Task 4: Local release assets, management smoke, and user documentation

**Files:**
- Modify: `tools/build_collection.py`
- Modify: `tests/contracts/test_production_package.py`
- Modify: `tests/integration/test_installer.py`
- Modify: `docs/user-installation.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `build_release(source, output, version) -> Path`, Task 3 `packaging/install.sh`, and all earlier wizard/bootstrap interfaces.
- Produces: `ReleaseAssets(archive: Path, checksum: Path, bootstrap: Path)`; `build_release_assets(source: Path, output: Path, version: str) -> ReleaseAssets`; a local clean-home smoke command documented for maintainers.

- [ ] **Step 1: Write failing release-asset tests**

Add tests named:

- `test_release_assets_include_archive_checksum_and_executable_bootstrap`;
- `test_release_checksum_uses_exact_archive_basename_and_bytes`;
- `test_release_assets_are_reproducible_and_preserve_existing_outputs`;
- `test_bootstrap_source_is_not_embedded_as_an_unverified_runtime_replacement`.

Assert `build_release` remains backward compatible and only the new `build_release_assets` creates sibling bootstrap/checksum assets.

- [ ] **Step 2: Run release-asset tests to verify they fail**

Run: `uv run --locked pytest tests/contracts/test_production_package.py -k release_assets -q`

Expected: FAIL because `ReleaseAssets` and `build_release_assets` do not exist.

- [ ] **Step 3: Implement deterministic local release assets**

Add frozen `ReleaseAssets` and `build_release_assets`. Copy `packaging/install.sh` byte-for-byte with mode `0755`, write `<archive-name>.sha256` atomically as lowercase SHA-256 plus two spaces plus archive basename, and refuse to overwrite any existing asset. Do not publish or contact GitHub.

- [ ] **Step 4: Run release contract tests**

Run: `uv run --locked pytest tests/contracts/test_production_package.py -q`

Expected: all production package tests PASS.

- [ ] **Step 5: Write failing full smoke and preservation tests**

Extend integration coverage with:

- `test_local_bootstrap_clean_home_install_to_human_doctor`;
- `test_wizard_update_preserves_previous_activation_on_failure`;
- `test_wizard_uninstall_preserves_modified_and_foreign_files`;
- `test_reconfigure_changes_only_new_session_defaults`.

The clean-home smoke builds local assets, exposes them through a local HTTP fixture, supplies a fake detected Codex executable, runs the shell bootstrap through a PTY, accepts defaults, and asserts the installed six skills plus successful doctor rows.

- [ ] **Step 6: Run smoke tests to verify they fail**

Run: `uv run --locked pytest tests/integration/test_installer.py -k 'local_bootstrap or wizard_update or wizard_uninstall or reconfigure' -q`

Expected: FAIL until the final orchestration and fixtures are connected.

- [ ] **Step 7: Complete orchestration and write friendly documentation**

Connect the local asset builder to the smoke fixture without adding a publishing workflow. Rewrite `docs/user-installation.md` around the future one-command journey, clearly label the public command as unavailable until a later publication task, document supported macOS Apple Silicon and prerequisite agent behavior, and move manual archive/flag details into an “Automation and development” section. Keep README’s primary installation link short and user-oriented.

- [ ] **Step 8: Run focused and full verification**

Run: `uv run --locked pytest tests/contracts/test_production_package.py tests/installer tests/integration/test_installer.py tests/integration/test_installer_shell.py -q`

Expected: focused installer/release suite PASS.

Run: `uv run --locked pytest -q`

Expected: complete suite PASS with no regressions.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 9: Commit**

```bash
git add tools/build_collection.py tests/contracts/test_production_package.py tests/integration/test_installer.py docs/user-installation.md README.md
git commit -m "docs: finish friendly installer workflow"
```

## Deferred Release Work

The following are intentionally not part of this plan: a GitHub Actions publishing
workflow, creation of a version tag or GitHub Release, upload of `install.sh` or
release assets, public URL smoke testing, and artifact signing. The local
`build_release_assets` output is the input to that later publication task.
