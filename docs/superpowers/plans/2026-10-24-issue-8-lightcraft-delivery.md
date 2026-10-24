# LightCraft Native Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переносить canonical Silverloupe selection в LightCraft по явным photo IDs с проверкой capability, preimage/readback, повторным apply и conflict-safe rollback.

**Architecture:** Новый `delivery/lightcraft.py` реализует host-mediated transport protocol: Python запрашивает discovery, asset mapping, metadata readback и batch execution только через переданный объект, не предполагая встроенного MCP. Общие `apply_delivery` и `rollback_delivery` диспетчеризуют `backend="native"`, `target="lightcraft"` в адаптер; XMP-путь остаётся без изменений.

**Tech Stack:** Python 3.12, Pydantic contracts, pytest, синхронный fake transport.

**Spec:** `docs/superpowers/plans/2026-10-22-silverloupe.md` (Task 8), `docs/superpowers/specs/2026-10-22-silverloupe-design.md` (section 9)

## Global Constraints

- Все mutations используют явные `ids`; текущий selection LightCraft не является входом.
- Existing user rating/flag/label не перезаписываются без ownership из предыдущего receipt.
- Proposed reject не превращается в `reject` flag без user/validated-policy authority.
- Color label применяется только через явный transport label mapping; универсальный цвет не предполагается.
- Planning и readback не изменяют develop settings.
- Отсутствующий MCP/capability даёт `report_only`, а не fallback writer.

## Review Focus

- Asset mapping указывает на уже исчезнувший или заменённый photo ID — test stale mapping conflict in Task 1.
- Два одинаковых `fileName` не должны создавать неоднозначность — test mapping is keyed by asset/photo IDs in Task 1.
- Частичный batch не должен повторно писать уже подтверждённые операции — test resume in Task 2.
- Неизвестные current flag/label нельзя нормализовать или затирать — test conflict/preservation in Task 2.
- Selection revision должна снять только owned fields и восстановить их baseline — test revised plan plus rollback in Task 2.

---

### Task 1: Contract-tested planning and explicit mapping

**Files:**
- Create: `tests/contracts/test_lightcraft_adapter.py`
- Create: `src/silverloupe/delivery/lightcraft.py`
- Modify: `src/silverloupe/delivery/__init__.py`

**Interfaces:**
- Consumes: `Manifest`, `list[SelectionItem]`, `list[Decision]`, transport methods `discover()`, `resolve(assets)`, `read(ids)`.
- Produces: `plan_lightcraft(manifest, items, decisions, transport, *, previous_receipt=None) -> DeliveryPlan`; `LightCraftTransport` protocol.

- [x] **Step 1: Write failing contract tests**

Cover capability absence, explicit ID mapping, identical names, stale/missing IDs, rating tiers, reject authority, and optional configured labels. Assert `report_only` for unavailable commands and conflicts without writes for stale mappings.

- [x] **Step 2: Run tests to verify RED**

Run: `uv run --locked pytest tests/contracts/test_lightcraft_adapter.py -q`
Expected: FAIL because `silverloupe.delivery.lightcraft` does not exist.

- [x] **Step 3: Implement deterministic planning**

Add `LightCraftTransport` and `plan_lightcraft(...)`; produce `AssetMapping.expected_before` from transport readback, `ProposedChange` entries for only safe/owned fields, and a deterministic `plan_id`. Keep all host results explicit and validate duplicate/unknown mappings.

- [x] **Step 4: Run contract tests to verify GREEN**

Run: `uv run --locked pytest tests/contracts/test_lightcraft_adapter.py -q`
Expected: PASS.

### Task 2: Apply, readback, resume, selection change, and rollback

**Files:**
- Create: `tests/integration/test_lightcraft_delivery.py`
- Modify: `src/silverloupe/delivery/lightcraft.py`
- Modify: `src/silverloupe/delivery/base.py`
- Modify: `src/silverloupe/delivery/__init__.py`

**Interfaces:**
- Consumes: ready native `DeliveryPlan`, optional prior receipt, transport `read(ids)` and `batch(operations)`.
- Produces: `apply_lightcraft(plan, transport) -> DeliveryReceipt`; `rollback_lightcraft(receipt, transport) -> DeliveryReceipt`; generic `apply_delivery(plan, transport=None)` and `rollback_delivery(receipt, transport=None)` dispatch.

- [x] **Step 1: Write failing integration tests**

Assert explicit IDs in every `photo.rate`, `photo.flag`, and configured `photo.label` operation; exact preimage checking; readback; preserved previous values; partial failure/resume; idempotent repeat; revised selection; unknown flag/label conflict; conflict-safe rollback; and no develop operation.

- [x] **Step 2: Run issue test command to verify RED**

Run: `uv run --locked pytest tests/contracts/test_lightcraft_adapter.py tests/integration/test_lightcraft_delivery.py -q`
Expected: FAIL on fake transport because apply/readback is not implemented.

- [x] **Step 3: Implement native transaction lifecycle**

Build grouped batch operations with explicit IDs, validate each preimage, record per-asset operation IDs and baseline/postimage ownership, read back every successful write, resume only missing work, and roll back only unchanged owned postimages.

- [x] **Step 4: Run issue tests and full suite**

Run: `uv run --locked pytest tests/contracts/test_lightcraft_adapter.py tests/integration/test_lightcraft_delivery.py -q`
Expected: PASS.

Run: `uv run --locked pytest -q`
Expected: PASS with no regression.

### Task 3: Adapter documentation and real-host probe

**Files:**
- Create: `resources/adapters/lightcraft.md`
- Modify: `skills/silverloupe-deliver/SKILL.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: verified adapter behavior and current LightCraft command discovery.
- Produces: host exchange instructions and an honest compatibility statement.

- [x] **Step 1: Document the explicit exchange**

Describe `photo.rate`, `photo.flag`, `photo.label`, ID mapping, capability discovery, readback, partial receipt/resume, selection independence, label configuration, and stop conditions.

- [x] **Step 2: Probe a disposable test photo in each available host**

Record command availability, explicit-ID mutation/readback, unchanged develop settings, and cleanup. If a host cannot be tested, state `contract-tested only` for that host.

- [x] **Step 3: Verify package and docs references**

Run: `uv run --locked pytest tests/contracts/test_skill_bundles.py tests/integration/test_package_resources.py -q`
Expected: PASS.
