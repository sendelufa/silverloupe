# Issue #9 Production Build and Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, eval-free Silverloupe release artifact and install it safely for Codex, Claude Code, or both with explicit editor/backend configuration.

**Architecture:** The release builder expands the existing closed skill collection into a strict allowlisted archive containing runtime source, locked production metadata, installer, adapters, templates, hashes, and ownership metadata. Installer modules keep capability probing, configuration, and filesystem lifecycle separate; updates stage and verify a complete tree before an atomic active-version switch, while uninstall removes only recorded owned files.

**Tech Stack:** Python 3.12, Pydantic 2, standard-library tar/gzip/hash/path/filesystem utilities, uv lock metadata, pytest.

**Spec:** `docs/distribution.md`, `docs/requirements.md`, `docs/superpowers/specs/2026-10-22-silverloupe-design.md`, and GitHub Issue #9.

## Global Constraints

- Production contains exactly six working skills and physically excludes `dev`, `evals`, `tests`, gold data, caches, internal plans, pytest, jsonschema, and developer-only dependency groups.
- Every runtime Markdown reference resolves inside the unpacked artifact; runtime imports must not resolve or import `evals`.
- The release archive is allowlist-built, hash-verified, reproducible for the same source/version, and never overwrites an existing artifact.
- Supported production platform is macOS Apple Silicon with Python `>=3.12,<3.13`; other platforms remain capability-gated rather than implied supported.
- `host-vision` installs no local model weights; RAW support is ready only when the selected renderer or compatible `rawpy` wheel is available.
- Native metadata mode never silently falls back to XMP; file mode is explicit, and unavailable integrations become `report-only` with a reason.
- Reconfiguration affects new sessions only: persisted `SessionConfig.editor_config_snapshot` and existing delivery plans remain unchanged.
- Updates prepare and verify a new version before switching; interruption leaves the previous active version intact.
- Uninstall removes only ownership-manifest entries and preserves sessions, photographs, results, user settings outside owned config, and foreign skills.
- Photo evals and release qualification run before publication and never on the photographer's machine.

## Review Focus

- Archive traversal, symlinks, undeclared files, and tampered checksums must fail closed; Task 1 pins these boundaries.
- Paths with spaces/Unicode and host skill directories containing foreign skills must install and uninstall without collateral changes; Task 3 pins them.
- Missing MCP/native capability must not be represented as ready or trigger implicit file writes; Task 2 pins explicit report-only behavior.
- A failed update after staging but before activation must preserve the previous config and active version; Task 3 injects this failure.
- Reinstalling the same release and switching default editor must be idempotent while old session snapshots remain byte-identical; Task 3 pins both cases.

---

### Task 1: Production Manifest and Deterministic Release Archive

**Files:**
- Create: `packaging/production-manifest.json`
- Create: `packaging/install.py`
- Modify: `tools/build_collection.py`
- Create: `tests/contracts/test_production_package.py`

**Interfaces:**
- Consumes: `build_collection(source_root, destination, kind="runtime")`, the project version/lock, six source skills, runtime package data, adapters, and user guide.
- Produces: `build_release(source: Path, output: Path, version: str) -> Path`; root `manifest.json` and `checksums.json` covering every shipped owned file.

- [ ] **Step 1: Write failing release composition tests**

Add tests for exactly six skills, expected root directories, closed Markdown references, runtime import without the dev checkout, no forbidden names/content/dependencies, matching hashes, no symlinks, deterministic bytes, non-overwrite behavior, and archive path traversal rejection.

- [ ] **Step 2: Run the contract tests and verify RED**

Run: `uv run --locked pytest tests/contracts/test_production_package.py -q`

Expected: FAIL because the production manifest, installer entry point, and `build_release` do not exist.

- [ ] **Step 3: Define the allowlist and bootstrap metadata**

Declare the six skills, runtime package/resource roots, adapters, template/profile resources, `uv.lock`, `pyproject.toml`, supported platform/host/editor combinations, production dependencies, bootstrap command, and forbidden path/dependency tokens. Keep the bootstrap entry point stdlib-only until it loads the installed runtime.

- [ ] **Step 4: Implement reproducible archive construction**

Implement `build_release(source: Path, output: Path, version: str) -> Path`. Stage only allowlisted bytes, generate sorted hashes/ownership, normalize archive mode/uid/gid/mtime, reject missing/symlink/escaping sources and existing outputs, validate the staged tree, then write `silverloupe-<version>.tar.gz` atomically.

- [ ] **Step 5: Run Task 1 tests and verify GREEN**

Run: `uv run --locked pytest tests/contracts/test_production_package.py -q`

Expected: all production package tests PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add packaging/production-manifest.json packaging/install.py tools/build_collection.py tests/contracts/test_production_package.py
git commit -m "feat: build deterministic production release"
```

### Task 2: Install Configuration and Backend Capabilities

**Files:**
- Modify: `src/silverloupe/contracts.py`
- Create: `src/silverloupe/installer/__init__.py`
- Create: `src/silverloupe/installer/config.py`
- Create: `src/silverloupe/installer/capabilities.py`
- Create: `tests/integration/test_installer.py`

**Interfaces:**
- Consumes: expanded `InstallConfig`, delivery target profiles, LightCraft transport discovery, runtime/platform facts, and optional RAW decoder availability.
- Produces: `configure_editor(config: InstallConfig, editor: str, mode: str) -> InstallConfig`; `probe_install(config: InstallConfig, *, probes: Mapping[str, object] | None = None) -> dict`; a `MetadataBackend` protocol exposing `capabilities()`, `plan(...)`, `apply(...)`, and `readback(...)` over `DeliveryPlan`/`DeliveryReceipt`.

- [ ] **Step 1: Write failing configuration and capability tests**

Cover Codex, Claude Code, and both hosts; multiple editors/default editor; native, file, and report-only modes; missing MCP/native backend; explicit XMP; host-vision without model weights; RAW renderer/wheel states; invalid host/editor/mode; and immutable copies returned by reconfiguration.

- [ ] **Step 2: Run Task 2 tests and verify RED**

Run: `uv run --locked pytest tests/integration/test_installer.py -q`

Expected: FAIL because installer modules and the expanded config contract do not exist.

- [ ] **Step 3: Expand the strict install contract**

Implement the distribution fields: `config_version`, `hosts`, `scope`, `install_root`, `enabled_editors`, `default_editor`, `integration_mode_by_editor`, `metadata_mapping_by_editor`, `runtime_version`, `model_mode`, `model_profile`, `cache_quota`, and `worker_limit`. Normalize legacy editor names only at the explicit configuration boundary, not silently inside delivery.

- [ ] **Step 4: Implement explicit configuration and probes**

`configure_editor` validates the editor/mode matrix and returns a model copy. `probe_install` reports `ready|degraded|unavailable` per host/editor/runtime/RAW/model capability with reasons and chosen metadata fields; native failures resolve to report-only, never a different writer.

- [ ] **Step 5: Run Task 2 tests and verify GREEN**

Run: `uv run --locked pytest tests/integration/test_installer.py -q`

Expected: all focused configuration/capability tests PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/silverloupe/contracts.py src/silverloupe/installer tests/integration/test_installer.py
git commit -m "feat: configure installer capabilities"
```

### Task 3: Transactional Install, Update, Reconfigure, and Uninstall

**Files:**
- Create: `src/silverloupe/installer/lifecycle.py`
- Modify: `packaging/install.py`
- Modify: `tests/integration/test_installer.py`
- Create: `docs/user-installation.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: a verified archive from Task 1 and configured/probed `InstallConfig` from Task 2.
- Produces: `install_release(artifact: Path, config: InstallConfig) -> dict`; `update_release(artifact: Path, config: InstallConfig, *, failpoint: str | None = None) -> dict`; `uninstall_release(config: InstallConfig) -> dict`; `load_installed_config(install_root: Path) -> InstallConfig`.

- [ ] **Step 1: Write failing clean-install lifecycle scenarios**

Test each host and both together, paths with spaces/Unicode, report-only without integration, skill registration, exact ownership, checksum rejection, malicious archive paths, idempotent reinstall, and no dependency on the source checkout.

- [ ] **Step 2: Write failing update/reconfigure/uninstall scenarios**

Test editor/default changes, byte-identical old session snapshots and delivery plans, staged-update failure preserving the active version, successful activation, and removal of only owned files while preserving foreign skills, user results, sessions, and external photographs.

- [ ] **Step 3: Run Task 3 tests and verify RED**

Run: `uv run --locked pytest tests/integration/test_installer.py -q`

Expected: FAIL because lifecycle functions are absent.

- [ ] **Step 4: Implement verified extraction and versioned activation**

Reject links, special files, absolute/traversing paths, duplicate members, undeclared files, and hash mismatches. Extract to a sibling staging directory, install runtime/skill trees into versioned owned locations, write config/ownership atomically, and switch a small active-version file only after all checks pass.

- [ ] **Step 5: Implement idempotence, safe update, and owned uninstall**

Detect same-version reinstall, preserve user-owned host entries, restore or remove only recorded registrations/files, tolerate already-missing owned files, and remove empty owned directories without traversing symlinks. Failure injection must clean staging and keep the previous active pointer/config.

- [ ] **Step 6: Implement the release CLI and user guide**

Support non-interactive host/editor/mode/install-root arguments plus interactive prompts when omitted. Document the uv-managed Python bootstrap command, capability/report-only output, RAW choices, update/reconfigure/uninstall behavior, supported platform statement, disk/RAM budgets, and the separate release publication action.

- [ ] **Step 7: Run all Issue #9 tests and verify GREEN**

Run: `uv run --locked pytest tests/contracts/test_production_package.py tests/integration/test_installer.py -q`

Expected: all Issue #9 tests PASS.

- [ ] **Step 8: Run the full regression suite and clean-artifact smoke**

Run: `uv run --locked pytest -q` and install a newly built archive into a temporary clean root with no source checkout on `sys.path`; run its packaged runtime import/doctor, reconfigure, update, and uninstall checks.

- [ ] **Step 9: Commit Task 3**

```bash
git add src/silverloupe/installer packaging/install.py tests/integration/test_installer.py docs/user-installation.md README.md
git commit -m "feat: install and manage production releases"
```
