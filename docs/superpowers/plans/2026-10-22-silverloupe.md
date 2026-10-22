# Silverloupe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task; superpowers:subagent-driven-development is an alternative when that execution method is authorized. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Построить проверяемую коллекцию скиллов для технического и художественного отбора фотографий с переносом разметки в Lightroom, Photoshop/Camera Raw и LightCraft.

**Architecture:** Шесть пользовательских скиллов используют одно Python-ядро и версионированные JSON-контракты; седьмой eval-скилл остаётся в dev-репозитории. Install-репозиторий генерируется из проверенного release, установщик выбирает host и backend редактора. Жанровые references загружаются по необходимости; визуальный анализ отделён от численных признаков и записи метаданных. Evals проверяют и качество отбора, и поведение коллекции в Codex/Claude Code.

**Tech Stack:** Python 3.12, uv, Pillow/NumPy, Pydantic, SQLite, pytest (dev-only), внешний ExifTool, RAW-extra rawpy либо проверенный рендер редактора; host-vision в MVP, отдельный API-provider позже.

**Spec:** [Дизайн](../specs/2026-10-22-silverloupe-design.md); [evals](../../evals.md); [исследование](../../research.md); [поставка и установщик](../../distribution.md); [требования к моделям/ресурсам](../../requirements.md); [HTML-review, итерация 2](../specs/2026-10-22-silverloupe-review-ui-design.md).

## Global Constraints

- Production-поставка не содержит eval-скилл, harness, тестовые данные и dev/eval зависимости.
- Установщик с выбором редактора входит в первую пользовательскую итерацию.
- Интерактивный HTML-review с альтернативами реализуется после первой итерации.
- Автоматическое удаление не входит в проект.
- Все схемы имеют `schema_version=1`.
- Manual overrides имеют приоритет и сохраняются при повторном прогоне.
- В MVP технический отсев — рекомендации.
- При недоступном vision выдаётся технический отчёт, художественный отбор остаётся `not_evaluated`.
- Не менять `.lrcat` напрямую и не пересоздавать XMP с нуля поверх существующей обработки.
- Совместимость подтверждается для конкретной версии/ОС/формата; Classic не означает cloud Lightroom.

План описывает будущие файлы и команды. Сейчас они ещё не реализованы. Пути ниже относительны корню репозитория. Оценка последовательности точнее оценки календарных сроков: главный внешний фактор — доступность фотографий, экспертной разметки и тестовых редакторов.

## Review Focus

1. Неподдерживаемый RAW и маленькое превью не превращаются в reject — задачи 2, 4.
2. HDR/панорама/focus stack не схлопываются как дубли — задачи 3, 5.
3. Единственный момент и намеренный смаз сохраняются в review/keep — задачи 4, 5.
4. XMP/ACR, одинаковые stems и параллельные пользовательские правки не повреждаются — задачи 7, 8.
5. Запуск из другой cwd, отсутствие vision и прерванный перенос не дают ложный успех — задачи 2, 6–10.

## Структура и общие интерфейсы

`src/silverloupe/contracts.py` определяет Pydantic-модели `Brief`, `Asset`, `Manifest`, `Group`, `SelectionItem`, `Assessment`, `Decision`, `Selection`, `RunConfig`, `RunState`, `DeliveryPlan`, `DeliveryReceipt`, `InstallConfig`, `SessionConfig`. `TrialResult` определяется в `evals/contracts.py` и не импортируется runtime. Их поля и enum соответствуют таблицам дизайна. `Manifest.assets` — список Asset, `RunState` хранит пути к артефактам и статус completed/partial/failed. Публичные функции перечислены в задачах; не дублировать схемы в скиллах.

`src/silverloupe/cli.py` предоставляет будущую команду `silverloupe`. Общие exit codes: 0 — завершено, 2 — неверный ввод/конфигурация, 3 — частичный результат или конфликт, 4 — ошибка среды/исполнения. stderr — диагностика, stdout — краткий JSON-результат; большие данные — файлы сессии.

Проверки ниже выполняются через `uv run --locked`. На каждом кодовом шаге: сначала конкретный падающий тест, затем минимальная реализация, затем успешная проверка. После самостоятельного завершённого результата — отдельный commit с названными файлами; коммитить реальные фото или generated reports нельзя.

## Задача 1. Контракты и минимальный eval-harness

**Files:** `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `src/silverloupe/{contracts,cli}.py`, `evals/{cases,rubrics}/`, `evals/dataset-manifest.schema.json`, `evals/runner.py`, `tests/contracts/test_models.py`, `tests/unit/test_eval_graders.py`.

**Consumes:** дизайн и программа evals. **Produces:** модели контрактов; `validate_run(path: Path) -> list[str]`; `grade_artifacts(case: dict, output: Path) -> dict`; `silverloupe doctor`.

- [ ] Зафиксировать 6 пилотных съёмок в manifest либо явно отметить real-data unavailable; подготовить разрешённые мини-fixtures для файловых проверок.
- [ ] Написать тесты: неизвестный asset_id недопустим; top вне selects недопустим; K считает SelectionItem, а не исходные файлы; proposed reject не экспортируется как флаг; unassessed не может иметь доказанную визуальную оценку; rating допустим null/0..5.
- [ ] Запустить `uv run --locked pytest tests/contracts tests/unit/test_eval_graders.py -q`; подтвердить падение на отсутствующих контрактах/graders.
- [ ] Реализовать схемы и deterministic graders; `doctor` сообщает Python/core/schema, ExifTool, RAW и vision capabilities без модификации библиотеки.
- [ ] Тем же запуском получить PASS; проверить, что reject-all и fake IDs проваливают соответствующие graders, валидный reference output проходит.

**Готово:** есть проверяемый контракт результата и измеритель ошибок до написания правил отбора.

## Задача 2. Инвентаризация, превью и восстановление

**Files:** `src/silverloupe/{inventory,previews,state}.py`, `tests/unit/test_inventory.py`, `tests/integration/test_prepare.py`.

**Consumes:** RunConfig/Manifest/Asset. **Produces:** `prepare(source: Path, output: Path, config: RunConfig) -> Manifest`; `render_preview(asset: Asset, config: RunConfig) -> Path`; CLI `prepare`, `status`, `resume`.

- [ ] Тестировать одинаковые имена в двух каталогах, одинаковые байты, Unicode/пробелы, неверный EXIF, orientation, RAW без thumbnail, неподдерживаемый RAW и исключение output из scan.
- [ ] Запустить `uv run --locked pytest tests/unit/test_inventory.py tests/integration/test_prepare.py -q`; получить ожидаемые ошибки отсутствующей реализации.
- [ ] Реализовать read-only inventory, content hashes, preview provenance и SQLite checkpoint; RAW fallback сохраняет decode_status вместо reject.
- [ ] Проверить PASS, неизменность хешей исходников, resume после прерывания, инвалидацию кэша после изменения файла и исполнение из чужой cwd.

**Готово:** одна команда даёт manifest/contact sheets и честный список неоценённых файлов; повторный запуск использует корректный кэш.

## Задача 3. Серии и защищённые комплекты

**Files:** `src/silverloupe/{grouping,comparisons}.py`, `tests/unit/test_grouping.py`, `evals/cases/grouping.jsonl`.

**Consumes:** Manifest. **Produces:** `group_assets(manifest: Manifest, config: RunConfig) -> list[Group]`; `make_comparison_batches(groups: list[Group], manifest: Manifest) -> list[dict]`; `build_selection_items(manifest: Manifest, groups: list[Group]) -> list[SelectionItem]`; CLI `group`.

- [ ] Написать тесты для RAW/JPEG пары, byte duplicates, burst, HDR, panorama, stack, пересекающихся связей, RAW+JPEG как одного item, HDR как одного composite с пятью required assets, конфликтующих composite-кандидатов, разных камер/часов и ложного совпадения stem.
- [ ] Запустить `uv run --locked pytest tests/unit/test_grouping.py -q` и подтвердить FAIL.
- [ ] Реализовать baseline времени/визуального сходства и protected-set semantics; неизвестный тип остаётся uncertain, не сжимается автоматически.
- [ ] Получить PASS и grouping report на пилоте; вручную проверить все ошибочные объединения, отдельно замерить количество сравнений на 1000/5000 файлов.

**Готово:** группы можно просмотреть и поправить; ни один комплект склейки не теряет членов.

## Задача 4. Техническая оценка и визуальные свидетельства

**Files:** `src/silverloupe/{signals,triage}.py`, `src/silverloupe/vision/{base,host_exchange}.py`, `resources/artistic-intent.md`, `tests/unit/test_triage.py`, `tests/contracts/test_vision_exchange.py`.

**Consumes:** Manifest, list[Group], Brief. **Produces:** `assess(manifest: Manifest, groups: list[Group], brief: Brief, config: RunConfig) -> list[Assessment]`; `propose_triage(assessments: list[Assessment], items: list[SelectionItem], brief: Brief) -> list[Decision]`; CLI `assess`, `vision-request`, `vision-import`.

- [ ] Создать cases: намеренный смаз, low-key, шум, глаза при поцелуе, единственный момент, маленькое preview и недоступный vision; assertions — отсутствие безусловного reject и корректная uncertainty.
- [ ] Проверить FAIL командой `uv run --locked pytest tests/unit/test_triage.py tests/contracts/test_vision_exchange.py -q`.
- [ ] Реализовать сигналы, обмен vision-request/response с observed artifact IDs, validation и ограниченную retry-политику: максимум 2 повтора transient ошибки, затем partial/review. Невалидный JSON не становится успешным результатом.
- [ ] Получить PASS; на пилотных фотографиях проверить причины и используемые кропы человеком. Поддельный ответ с несуществующим observed ID должен отвергаться, а реальные tool traces позднее проверят факт просмотра.

**Готово:** технические рекомендации объяснимы; отсутствие свидетельств видно; auto-reject пока выключен.

## Задача 5. Жанровая редактура и два набора результата

**Files:** `src/silverloupe/curation.py`, `resources/genres/{wedding,architecture,landscape}.md`, `resources/profiles/{wedding,architecture,landscape}.json`, `tests/unit/test_curation.py`, `evals/cases/curation.jsonl`.

**Consumes:** Manifest, Group, SelectionItem, Assessment, Brief, пользовательские overrides. **Produces:** `curate(manifest: Manifest, groups: list[Group], items: list[SelectionItem], assessments: list[Assessment], brief: Brief, overrides: list[Decision]) -> tuple[list[Decision], Selection]`; CLI `curate`.

- [ ] Тестировать ties, несколько допустимых победителей, select/top, top⊆selects, разнообразие, mandatory coverage, K=0/1/больше набора, невозможные ограничения, K по item IDs, раскрытие composite без потери required assets и приоритет overrides.
- [ ] Запустить `uv run --locked pytest tests/unit/test_curation.py -q` и подтвердить FAIL.
- [ ] Реализовать сравнительные оценки и ограниченный выбор набора; unknown genre не использует портретные правила; каждый профиль содержит исключения и отрицательные примеры.
- [ ] Получить PASS; измерить burst-choice/nDCG/coverage на dev. При отсутствии экспертных labels статус фото-eval остаётся unavailable.

**Готово:** свадьба даёт последовательную историю, архитектура — нужные виды, пейзаж — выразительный разнообразный топ; эти утверждения принимаются только после соответствующих фото-evals.

## Задача 6. Скиллы, disclosure и рабочий отчёт

**Files:** шесть `skills/*/SKILL.md` из дизайна, dev-only `dev/skills/silverloupe-eval/SKILL.md`, `resources/contracts.md`, `tools/build_collection.py`, `src/silverloupe/{report,workflow}.py`, `tests/integration/test_workflow.py`, `tests/contracts/test_skill_bundles.py`, `evals/cases/routing.jsonl`.

**Consumes:** функции задач 1–5 и их артефакты. **Produces:** `run_session(config: RunConfig) -> RunState`; `build_report(state: RunState) -> Path`; CLI `run`, `report`, `apply-overrides`; сборка runtime-коллекции и отдельного dev-toolkit.

- [ ] До написания инструкций запустить B2 без скилла на контрольных запросах и сохранить реальные ошибки как baseline.
- [ ] Написать проверки bundles: frontmatter, все ссылки существуют, runtime dependency объявлена, single-source ресурсы совпадают, production allowlist не включает dev-скилл/ссылки на evals; integration — partial report и manual corrections round-trip.
- [ ] Запустить `uv run --locked pytest tests/contracts/test_skill_bundles.py tests/integration/test_workflow.py -q`, подтвердить FAIL.
- [ ] Написать компактные инструкции с независимыми триггерами и условными ссылками; сгенерировать bundles. Статический отчёт показывает top/selects/review/reject/reserve, причины, сравнения и provenance. Правки через агента/CLI используют валидируемый overrides JSON с item_id, revision и selection scope. Интерактивные карточки, свайпы и сервер review сюда не входят.
- [ ] Получить PASS; проверить HTML визуально и пройти один end-to-end сценарий в Codex и Claude Code, включая иной cwd и недоступный vision. Записать traces чтения references.

**Готово:** первый вертикальный MVP даёт пригодный к проверке отбор и отчёт в обоих хостах. До задач 7–8 реальный экспорт редакторам не объявляется поддержанным.

## Задача 7. Lightroom / Photoshop: перенос метаданных

**Files:** `src/silverloupe/delivery/{base,xmp,adobe}.py`, `resources/adapters/{lightroom,photoshop}.md`, `tests/integration/test_xmp_roundtrip.py`, `tests/integration/test_delivery_resume.py`, `evals/compatibility-matrix.json`.

**Consumes:** Manifest, SelectionItem, Decision, Selection и выбранный профиль редактора. **Produces:** `plan_delivery(manifest: Manifest, items: list[SelectionItem], decisions: list[Decision], target: str) -> DeliveryPlan`; `apply_delivery(plan: DeliveryPlan) -> DeliveryReceipt`; `rollback_delivery(receipt: DeliveryReceipt) -> DeliveryReceipt`; CLI `delivery-plan`, `delivery-apply`, `delivery-rollback`.

- [ ] Сначала проверить доступный backend приложения и его поля: native-интеграция либо явно настроенный XMP exchange. Отсутствие native capability не переключает writer молча. Затем compatibility probe на копиях RAW/JPEG/TIFF/DNG/PSD: фактический способ записи/чтения, сохранность develop/ACR, flags и пользовательских color label names. Неизвестное = not tested.
- [ ] Написать tests сохранения неизвестных XMP полей, совпадающих stems, changed preimage, partial write, запрета reject-флага для decision_authority=proposal, batch-принятия списка reject с проверкой хеша, повторного apply, ownership полей/keywords, top 5★ → select 4★ после замены, восстановления исходного значения и conflict-safe rollback.
- [ ] Запустить `uv run --locked pytest tests/integration/test_xmp_roundtrip.py tests/integration/test_delivery_resume.py -q`, подтвердить FAIL.
- [ ] Реализовать field-level merge, staging, backups, журнал и readback; неподтверждённый формат отдаёт report/copy workflow, не рискованную запись оригинала.
- [ ] Получить PASS и реальный Adobe round-trip на тестовом каталоге/Bridge/Camera Raw; зафиксировать версии и доказательства в compatibility matrix.

**Готово:** поддержка объявлена только для проверенных сочетаний. Photoshop получает выбранные файлы через проверенный Bridge/Camera Raw маршрут.

## Задача 8. LightCraft: нативная разметка

**Files:** `src/silverloupe/delivery/lightcraft.py`, `resources/adapters/lightcraft.md`, `tests/contracts/test_lightcraft_adapter.py`, `tests/integration/test_lightcraft_delivery.py`.

**Consumes:** DeliveryPlan/Receipt; обнаруженные capabilities MCP. **Produces:** `plan_lightcraft(manifest: Manifest, items: list[SelectionItem], decisions: list[Decision], transport: object) -> DeliveryPlan`; адаптер в общем apply/readback механизме задачи 7.

- [ ] Тестировать explicit IDs, stale mapping, одинаковые имена, смену selection, частичный сбой, повторную запись, неизвестный flag/label и отсутствие MCP.
- [ ] Запустить `uv run --locked pytest tests/contracts/test_lightcraft_adapter.py tests/integration/test_lightcraft_delivery.py -q`, подтвердить FAIL на fake transport.
- [ ] Реализовать capability check, mapping и пакетные операции `photo.rate`, `photo.flag`, `photo.label` с readback и сохранёнными предыдущими значениями. Transport получает host tool results через явный обмен; Python не предполагает встроенного доступа к инструментам агента.
- [ ] Получить PASS; проверить реальную интеграцию на тестовых фотографиях, отдельно в каждом доступном хосте. Без реального теста — contract-tested only.

**Готово:** одинаковый canonical отбор переносится в LightCraft без зависимости от текущего выделения и без изменения проявки.

## Задача 9. Production-сборка и установщик

**Files:** `src/silverloupe/installer/{config,capabilities,lifecycle}.py`, `packaging/{production-manifest.json,install.py}`, `tools/build_collection.py`, `tests/contracts/test_production_package.py`, `tests/integration/test_installer.py`, `docs/user-installation.md`.

**Consumes:** production-скиллы задачи 6, backend capabilities задач 7–8, InstallConfig/SessionConfig. **Produces:** `build_release(source: Path, output: Path, version: str) -> Path`; `install_release(artifact: Path, config: InstallConfig) -> dict`; `configure_editor(config: InstallConfig, editor: str, mode: str) -> InstallConfig`; lifecycle update/uninstall согласно distribution. MetadataBackend задаёт `capabilities()`, `plan(...)`, `apply(...)`, `readback(...)` поверх DeliveryPlan/Receipt.

- [ ] Написать тесты состава архива: ровно шесть рабочих скиллов, отсутствие evals/tests/gold/dev-зависимостей, все runtime references доступны, импорт runtime не обращается к evals.
- [ ] Написать сценарии чистой установки в оба хоста/оба одновременно, выбора и смены редактора, отсутствия MCP/native backend, режима report-only, повторной установки, обрыва обновления и удаления только owned files. Session snapshot сохраняет прежний editor; новый default не меняет старый delivery-plan.
- [ ] Запустить `uv run --locked pytest tests/contracts/test_production_package.py tests/integration/test_installer.py -q` и подтвердить FAIL на отсутствующей сборке/установщике.
- [ ] Реализовать allowlist-сборку install-репозитория/архива, manifest/hashes, production environment и мастер host/editor/mode. Установить только выбранные зависимости/интеграции; не запускать фото-evals на устройстве пользователя. Нативный backend использует приложение; файловый режим выбирается явно. Проверить bootstrap без заранее настроенного Python, wheel availability, RAW camera support, model/host image smoke и resource budget согласно requirements; не скачивать локальные модельные веса в host-vision режиме.
- [ ] Получить PASS; установить собранный архив в чистую среду без dev-checkout. Пройти отбор и перенос через каждый заявленный backend; повторить configure/update/uninstall, проверить сохранность настроек, результатов и чужих скиллов.

**Готово:** фотограф устанавливает готовый продукт, выбирает программу и получает работающий поддержанный путь записи метаданных без eval-инструментов. Публикация удалённого install-репозитория выполняется отдельным release-действием из этой же сборки.

## Задача 10. Release evals, оптимизация и поставка

**Files:** `evals/{runner,metrics,host_adapters}.py`, `evals/baselines/`, `tests/unit/test_metrics.py`, `tools/build_collection.py`, `docs/compatibility.md`, `docs/release-report.md`.

**Consumes:** production-артефакт задачи 9 и sealed cases; harness запускается отдельно в dev/CI. **Produces:** `run_trial(case: dict, host: str, variant: str, output_dir: Path) -> TrialResult`; `summarize_trials(results: list[TrialResult]) -> dict`; версионированная сборка и release report.

- [ ] Проверить формулы метрик на маленьких известным образом размеченных результатах, включая пустые знаменатели; агент не имеет доступа к gold.
- [ ] Запустить `uv run --locked pytest tests -q`; ожидается PASS, без выдачи mocked tests за live-host evals.
- [ ] Выполнить routing/disclosure suites по 3 trials; сравнение B0–B4 и ablations; фото-release по 5 trials; editor round-trip по матрице. Команда будущего runner: `uv run --locked python -m evals.runner --suite release --hosts codex,claude --trials 5 --output runs/release`.
- [ ] Измерить cold/warm p50/p95 и time-to-approved-selection на 1000 и 5000 файлов при фиксированном железе/режиме; записать peak RSS, disk, vision batches и стоимость; проверить целевые 8/16/32 ГБ, 1/2 workers и 24/45/60+ MP из requirements. Утверждённый минимум ресурсов появляется только после измерений. Оптимизировать только выявленные узкие места, затем повторить затронутые проверки.
- [ ] Проверить именно release-артефакт без dev checkout, его происхождение/состав и версионные несовместимости runtime/schema; написать support matrix и ограничения. Plugin manifests добавлять после локальной совместимости, отдельным изменением.
- [ ] Принять release decision по gates из evals. Если статистики мало, выпустить recommendation-only; auto-reject оставить выключенным. Сохранить воспроизводимую команду, версии, counts и CI в отчёте.

**Готово:** установочная коллекция с проверенными сценариями, известными ограничениями и измеримой пользой относительно baseline.

## Задача 11. HTML-review с альтернативами — следующая итерация

**Files:** `resources/templates/review/{index.html,styles.css,app.js}`, `src/silverloupe/review/{contracts,events,server,alternatives}.py`, `tests/integration/test_review_actions.py`, `tests/browser/`, production manifest новой версии.

**Consumes:** SelectionItem, Selection, manual overrides/revision, comparisons и выбранный MetadataBackend. **Produces:** `apply_review_action(action: ReviewAction, state: RunState) -> RunState`; `list_alternatives(item_id: str, state: RunState) -> list[str]`; `render_review(state: RunState) -> Path`; CLI `review`. ReviewAction и переходы описаны в отдельном дизайне HTML-review.

- [ ] Написать тесты accept/exclude/skip/replace/undo для top и selects, top⊆selects, различия exclude и technical reject, отсутствия альтернатив и сохранения composite required assets; последовательности exclude → accept → undo и apply → undo → apply с postimage-конфликтом.
- [ ] Добавить тесты повтора action_id, stale revision, reload/restart, несохранённого события при ошибке, ограничений доступа локального сервера и экранирования EXIF/имён. Запустить `uv run --locked pytest tests/integration/test_review_actions.py -q`, подтвердить FAIL.
- [ ] Реализовать версионированный шаблон, локальный сервер и журнал событий. Карточки поддерживают swipe/кнопки/клавиатуру, zoom, сравнение и альтернативы. Каждый сохранённый выбор — override, не запись во внешний каталог.
- [ ] Подключить отдельное применение принятой revision через выбранный metadata backend; для host-mediated MCP показывать pending до receipt. Проверить partial failure/resume и отсутствие изменений редактора от простого свайпа.
- [ ] Получить PASS, выполнить browser tests desktop/touch/keyboard-only и ручную проверку на реальной серии; сравнить время и ошибки со статическим отчётом. Только после этого добавить template/server зависимости в production-поставку новой версии.

**Готово:** интерактивная проверка результата с сохранением прогресса и подбором альтернатив. Задача не блокирует первую пользовательскую версию и не выполняется в её объёме.

## Порядок поставки и контроль масштаба

| Веха | Задачи | Проверяемая ценность |
|---|---|---|
| M0 | 1 | Контракты и измеритель, проверенные эталонным и плохими решениями |
| M1 | 2–6 | Вертикальный recommendation-only MVP на пилотной свадьбе, затем профили остальных жанров |
| M2 | 7–8 | Разметка в конкретных проверенных редакторах |
| M3 — первая пользовательская итерация | 9–10 | Установщик, выбор редактора, production без evals и проверенная поставка |
| M4 — следующая итерация | 11 | HTML-review с принятием/отклонением и альтернативами |

Сначала собрать тонкий свадебный сценарий задач 2–6; не ждать полного ML-покрытия трёх жанров для первого пользовательского результата. Архитектурные/пейзажные fixtures и защита merge sets должны существовать с начала, даже если расширенные рейтинги этих жанров принимаются позже.

Задача 11 уже предусмотрена следующей итерацией по запросу пользователя. Другие расширения после M3, при доказанной необходимости: локальная vision-модель, API-provider для unattended batch, авторские preference-профили, Lightroom catalog plugin, другие ОС и plugin distribution. Эти расширения получают отдельные планы; не раздувать ими MVP.

## Самопроверка плана

Покрытие требований: брак — задача 4; разметка — 7/8; серии — 3; жанры и художественные приёмы — 4/5; top — 5; disclosure/оба агента — 6/9/10; dev/install и установщик — 9; HTML-review — 11; uv — 1 и все команды; evals — 1/10 плюс проверки каждого этапа. Review Focus привязан к соответствующим тестам. Реальные данные и приложения — зависимости для доказательства качества, не препятствие написанию ядра и контрактов.

Следующий практический шаг после принятия дизайна — задача 1 и небольшой пилот реальной съёмки. Этот документ завершает запрос на планирование; установка зависимостей, реализация скиллов и изменение фотобиблиотек в текущую работу не входят.

Первоначальный независимый builder-check выполнен; внесены уточнения о SelectionItem/K, принятии reject и статистическом gate при нуле ошибок. Эти решения являются предложениями автора плана, а не уже согласованными настройками пользователя.
