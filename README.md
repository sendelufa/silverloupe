# Silverloupe

> Every frame deserves a closer look.

Silverloupe — проект проверяемого ассистированного отбора фотографий для Codex и Claude Code. Цель — помочь фотографу разобрать съёмку, сохранить значимые кадры и защищённые комплекты, сформировать рабочий отбор и топ, а затем безопасно перенести подтверждённую разметку в Lightroom, Photoshop / Camera Raw или LightCraft.

## Текущий статус

**Реализован полный recommendation workflow и безопасное транзакционное ядро доставки:** read-only подготовка, консервативная группировка, проверяемый host-vision exchange, жанровая редактура, статический отчёт, revision-bound ручные исправления и contract-tested XMP sidecar plan/apply/rollback. Совместимость конкретных версий Adobe остаётся `not-tested`, пока нет реального application round-trip.

| Возможность | Статус |
|---|---|
| Версионированные Pydantic-контракты (`schema_version=1`) | Готово |
| Валидация run-артефактов и deterministic graders | Готово |
| Проверки fake IDs, полноты учёта, top-K и reject safety | Готово |
| `silverloupe doctor` | Готово |
| Разрешённый mini-fixture и manifest eval-данных | Готово |
| Read-only инвентаризация и content SHA-256 | Готово |
| Нормализованные превью и contact sheets | Готово |
| SQLite checkpoint, `status` и `resume` | Готово |
| Группировка серий, RAW+JPEG, HDR, panorama и focus stack | Готово |
| Защищённые composite-наборы и единицы отбора | Готово |
| Локальные технические сигналы и безопасный triage | Готово |
| Проверяемый `vision-request` / `vision-import` | Готово |
| Жанровая редактура и итоговые `selects` / `top` | Готово (качество на реальных фото ещё не подтверждено) |
| Статический отчёт и revision-bound ручные исправления | Готово |
| Production-набор из шести скиллов и отдельный dev-toolkit | Готово |
| XMP sidecar plan/apply/readback/rollback для proprietary RAW | Готово на contract fixtures |
| Lightroom Classic / Photoshop / Bridge application round-trip | `not-tested`; report-only до реального probe |
| Нативная разметка LightCraft | Запланировано в Issue #8 |

Актуальные этапы и критерии готовности находятся в [плане разработки](docs/superpowers/plans/2026-10-22-silverloupe.md) и [GitHub Issues](https://github.com/sendelufa/silverloupe/issues).

## Быстрый запуск

### Требования

- [uv](https://docs.astral.sh/uv/);
- Git;
- macOS, Linux или Windows с поддержкой Python 3.12.

Версия Python зафиксирована в `.python-version`, зависимости — в `uv.lock`. Создавать virtualenv вручную не нужно.

```bash
git clone https://github.com/sendelufa/silverloupe.git
cd silverloupe
uv sync --locked
```

Проверьте окружение:

```bash
uv run --locked silverloupe doctor
```

Команда выводит компактный JSON и ничего не изменяет в библиотеке пользователя. Она сообщает состояние:

- Python runtime;
- версии Silverloupe core и схем;
- ExifTool;
- RAW backend (`rawpy`);
- host-vision capability.

Статусы `degraded` и `unavailable` — диагностический результат, а не скрытый успех. Например, отсутствие ExifTool или vision будет явно отражено в JSON.

### Первый пользовательский workflow

Создайте строгий brief JSON. Неизвестные поля отклоняются, а отсутствующие события, люди и требования не додумываются:

```json
{
  "schema_version": 1,
  "genres": ["wedding"],
  "purpose": "client story",
  "top_k": 40,
  "coverage_checklist": ["ceremony", "portraits", "reception"]
}
```

Запустите полный безопасный pipeline одной командой:

```bash
uv run --locked silverloupe run /path/to/photos /path/to/output --brief /path/to/brief.json
```

Код `0` означает завершённый отбор. Код `3` означает честный `partial`: подготовка, оценки, `vision-request.json`, `run-state.json` и static report уже доступны, но `selection.json` не публикуется как завершённый результат. Просмотрите только перечисленные в запросе превью, сформируйте строгий response и импортируйте его:

```bash
uv run --locked silverloupe vision-import /path/to/output /path/to/vision-response.json
uv run --locked silverloupe run /path/to/photos /path/to/output --brief /path/to/brief.json
```

Повторно собрать отчёт из сохранённого состояния можно без анализа фотографий:

```bash
uv run --locked silverloupe report /path/to/output
```

Отчёт находится в `output/report/index.html`. Это static, полностью локальный и неинтерактивный HTML без сервера, карточек, свайпов и state-changing JavaScript. В нём всегда есть разделы Top, Selects, Review, Reject и Reserve, причины, alternatives, provenance, coverage, constraints и честные ограничения photo-eval.

Для ручной коррекции создайте JSON-массив с текущим `revision` из `session.json`:

```json
[
  {
    "schema_version": 1,
    "item_id": "item-id",
    "revision": 2,
    "selection_scope": "top",
    "action": "exclude",
    "reason": "client prefers the alternate expression"
  }
]
```

Примените правку и перестройте отчёт:

```bash
uv run --locked silverloupe apply-overrides /path/to/output /path/to/overrides.json
```

Исключение из `top` переводит item в selects, исключение из `selects` — в reserve. Ни одно из этих действий не создаёт технический reject. Правка привязана к revision, получает user authority и сохраняется при повторном `run`.

### Перенос метаданных

Сначала создайте не изменяющий файлы план с явно выбранным backend:

```bash
uv run --locked silverloupe delivery-plan /path/to/output --target xmp-sidecar
```

Проверьте `delivery-plan.json`, особенно `conflicts`. `xmp-sidecar` — contract-tested низкоуровневый backend только для соседних sidecar проприетарных RAW. Он не означает, что установленная версия Lightroom, Photoshop, Bridge или Camera Raw уже проверена. Целевые профили Adobe остаются `not-tested` и дают report-only plan до реального round-trip.

Применение и безопасный откат выполняются отдельно:

```bash
uv run --locked silverloupe delivery-apply /path/to/output
uv run --locked silverloupe delivery-rollback /path/to/output
```

Apply сверяет хеши RAW и XMP, сохраняет неизвестные XMP/develop-поля, создаёт staging, backup, journal и `delivery-receipt.json`, затем проверяет readback. Batch не притворяется атомарным: partial можно повторить. Rollback — conflict-safe и не затирает последующую правку редактора или пользователя.

Для пересмотренного отбора постройте новый план с `--previous-receipt`; только принадлежащие Silverloupe значения могут быть обновлены или сняты. Proposed reject не становится reject-значением без user/validated-policy authority либо точного `--accept-reject-hash`. JPEG, TIFF, DNG и PSD не изменяются на месте и пока идут по report/copy workflow.

### Production skills и dev-toolkit

Production runtime содержит ровно шесть скиллов: `silverloupe`, `silverloupe-prepare`, `silverloupe-triage`, `silverloupe-compare`, `silverloupe-curate` и `silverloupe-deliver`. Их ссылки замкнуты внутри сборки.

Отдельный dev-toolkit содержит только `silverloupe-eval`, routing corpus и baselines. Он не устанавливается фотографу и не попадает в production runtime.

Собрать артефакты можно в пустые каталоги:

```bash
uv run --locked python tools/build_collection.py . dist/runtime --kind runtime
uv run --locked python tools/build_collection.py . dist/dev-toolkit --kind dev-toolkit
```

Builder использует allowlist, отклоняет symlink/traversal, переписывает ссылки в bundle-local references и создаёт детерминированный `manifest.json` с SHA-256.

### Подготовить свою папку

Команда рекурсивно читает поддерживаемые изображения, но не изменяет оригиналы:

```bash
uv run --locked silverloupe prepare /path/to/photos /path/to/silverloupe-output
```

Размер длинной стороны превью можно ограничить явно:

```bash
uv run --locked silverloupe prepare /path/to/photos /path/to/silverloupe-output --max-edge 1600
```

Проверить состояние или продолжить прерванный запуск:

```bash
uv run --locked silverloupe status /path/to/silverloupe-output
uv run --locked silverloupe resume /path/to/silverloupe-output
```

Сгруппировать подготовленную сессию:

```bash
uv run --locked silverloupe group /path/to/silverloupe-output
```

Для ручной коррекции отредактируйте `groups.json`, выставьте исправленным группам `confidence_kind: "manual"`, затем перестройте только единицы отбора без повторного автоанализа:

```bash
uv run --locked silverloupe group /path/to/silverloupe-output --reuse-groups
```

Обычный повторный `group` сохраняет группы с `confidence_kind: "manual"` и пересчитывает только автоматические связи вокруг них. Чтобы намеренно удалить ручные исправления и начать автоматическую группировку заново, используйте явный сброс:

```bash
uv run --locked silverloupe group /path/to/silverloupe-output --reset-manual-groups
```

Пересекающиеся защищённые composite-группы команда отклонит: сначала конфликт нужно разрешить в `groups.json` или перевести связь в `uncertain`.

### Получить техническую оценку

После `group` измерьте локальные сигналы:

```bash
uv run --locked silverloupe assess /path/to/silverloupe-output
```

Пока визуальные свидетельства не импортированы, команда завершится кодом `3` и статусом `partial`. Это ожидаемое безопасное состояние: численные признаки записаны, но не объявлены доказательством резкости, намерения или состояния глаз. Автоматический reject в MVP всегда выключен.

Для визуальной проверки сформируйте обменный файл:

```bash
uv run --locked silverloupe vision-request /path/to/silverloupe-output
```

`vision-request.json` содержит только безопасные относительные пути превью, стабильные `artifact_id`, полный список критериев, номер попытки и `input_fingerprint`. Fingerprint связывает запрос с content hash оригинала, фактическими байтами превью, render policy и brief: ответ на устаревший запрос импортировать нельзя. Передайте файл host-агенту Codex/Claude вместе с перечисленными изображениями. Ответ должен ссылаться только на фактически просмотренные `artifact_id` и вернуть результат по **каждому** запрошенному критерию:

```json
{
  "schema_version": 1,
  "request_id": "vision-…",
  "status": "completed",
  "observations": [
    {
      "schema_version": 1,
      "asset_id": "…",
      "observed_artifact_ids": ["overview-…"],
      "visual_criteria": {
        "focus_on_relevant_subject": "acceptable",
        "motion_blur_and_intent": "acceptable",
        "significant_region_exposure": "acceptable",
        "eye_state_when_contextually_relevant": "not_applicable",
        "accidental_subject_occlusion": "acceptable",
        "artistic_intent_hypotheses": "acceptable"
      },
      "region_evidence": [],
      "intent_hypotheses": [],
      "uncertainty": []
    }
  ],
  "error": null
}
```

Импортируйте ответ:

```bash
uv run --locked silverloupe vision-import \
  /path/to/silverloupe-output \
  /path/to/vision-response.json
```

Допустимые значения критерия: `acceptable`, `concern`, `uncertain`, `not_applicable` и `insufficient_evidence`. Запрос включает нейтральные редакторские критерии, критерии известных жанров и отдельный ключ `coverage:<label>` для каждого пункта `coverage_checklist`; `acceptable` у такого ключа означает, что пункт видимо представлен в кадре. Последнее значение честно оставляет asset в `partial/review` и позволяет сформировать новый запрос. Несуществующий observed ID, чужой asset, неполный набор критериев, `NaN`, изменившиеся входы или region evidence без просмотренного artifact отклоняются без замены прежних оценок. Для `transient_error` допускаются не более двух повторов; после исчерпания попыток результат остаётся `partial/review`.

Повторный `assess` сохраняет валидные визуальные свидетельства и решения с `decision_authority: "user"` / `user_override: true`. Если оригинал, превью, brief или render policy изменились, устаревшее vision-свидетельство сбрасывается, а `vision-status.json` получает `invalidated`. Любая повторная публикация assessment/vision также помечает прежний `curation-generation.json` как `invalidated`, поэтому старые `selection.json` и новые решения не выглядят единым завершённым снимком. Код `0` возвращается только тогда, когда оценены все assets; наличие неподдерживаемого RAW сохраняет общий статус `partial` и код `3`.

### Сформировать `selects` и `top`

После оценки запустите жанровую редактуру:

```bash
uv run --locked silverloupe curate /path/to/silverloupe-output
```

Команда читает `Brief.genres`, загружает только известные профили `wedding`, `architecture` и `landscape`, сравнивает `SelectionItem` по нормированным свидетельствам и формирует широкий рабочий `selects` и упорядоченный `top`. Неизвестный жанр использует только нейтральные критерии и не получает свадебные или портретные правила.

`top_k` считается по item IDs: защищённый HDR, panorama или focus stack занимает одно место, а его `required_asset_ids` сохраняются в provenance решения. `top_ids` всегда входит в `selects_ids`. Обязательное покрытие из `coverage_checklist`, недостаток кандидатов, конфликт coverage с K и ручной top сверх K записываются в `unmet_constraints`, а не скрываются подбором несуществующего результата. Штраф разнообразия уменьшает повторы из burst/near-duplicate групп, не превращая проигравшие варианты в технический reject. `repetition_limits` адресует существующие `group_id` и задаёт максимальное число items этой группы в автоматическом top; неизвестные группы отклоняются, а ручное превышение сохраняется и отмечается как `repetition_limit:<group_id>`.

Решения с `decision_authority: "user"` или `user_override: true` имеют приоритет при повторном запуске. `curate` сохраняет их поля и членство top/select/reserve/none; forced top автоматически остаётся внутри selects и учитывается при расчёте разнообразия. Технические причины `review`, включая визуальный `concern` и `unique_moment`, сохраняются рядом с редакторским tier. Три JSON-профиля версионированы отдельно, включены в устанавливаемый wheel, а соответствующие гайды в `resources/genres/` описывают художественные исключения и отрицательные примеры.

Синтетические unit/eval-кейсы доказывают только контракт, детерминированность и обработку ограничений. Метрики burst-choice, nDCG и фотографического coverage остаются `unavailable`, пока нет разрешённых экспертных labels; утверждения о качестве свадебного, архитектурного или пейзажного отбора до фото-evals не делаются.

Если output находится внутри исходной папки, он автоматически исключается из повторного scan. Output не может совпадать с source или быть его родительским каталогом. Silverloupe отклоняет симлинки на своих служебных путях до записи, чтобы генерация и очистка не могли затронуть оригиналы.

Одинаковые имена и одинаковые байты в разных каталогах остаются отдельными assets; content hash хранится независимо. JPEG/PNG/TIFF/WebP и другие форматы Pillow получают нормализованное превью. RAW использует optional `rawpy`; без доступного декодера файл остаётся `unsupported/unassessed`, но не превращается в reject. Ключ кэша учитывает content hash, rendering policy, версию renderer и доступность RAW backend, а повреждённые превью автоматически пересоздаются при `resume`.

### Запуск тестов

```bash
uv run --locked pytest -q
```

Контрактный набор отдельно:

```bash
uv run --locked pytest tests/contracts tests/unit/test_eval_graders.py -q
```

## Что доступно сейчас

### Контракты данных

Модели находятся в [`src/silverloupe/contracts.py`](src/silverloupe/contracts.py) и используют строгую валидацию Pydantic. Определены:

- `Brief`, `Asset`, `Manifest`, `Group` и `SelectionItem`;
- `Assessment`, `Decision` и `Selection`;
- `RunConfig`, `RunState` и `SessionConfig`;
- `DeliveryPlan`, `DeliveryReceipt` и `InstallConfig`.

Основные инварианты:

- все схемы имеют `schema_version=1`;
- `top_ids` всегда является подмножеством `selects_ids`;
- `rating_proposal` допускает только `null` или целое значение `0..5`;
- `unassessed` не может содержать доказанную визуальную оценку;
- ссылки на неизвестные `asset_id` и `item_id` недопустимы;
- K считается по `SelectionItem`, а не по количеству исходных файлов;
- proposed reject нельзя экспортировать как reject-флаг без разрешённого authority.

### Артефакты подготовки

После `prepare` создаются только рабочие файлы внутри указанного output:

```text
silverloupe-output/
├── session.json
├── assets.jsonl
├── state.sqlite3
├── groups.json
├── selection-items.json
├── grouping-report.json
├── assessments.jsonl
├── decisions.jsonl
├── assessment-report.json
├── assessment-generation.json # publishing / complete для согласованного snapshot
├── vision-request.json     # появляется после vision-request
├── vision-status.json      # pending / retry_required / completed / partial / invalidated
├── selection.json          # item-level selects, ordered top, coverage и ограничения
├── curation-report.json    # профили, counts и честный статус photo-eval
├── curation-generation.json # publishing / complete для согласованного snapshot
├── previews/              # JPEG + provenance sidecars
└── contact-sheets/
```

`assets.jsonl` содержит относительный путь, отдельные `asset_id` и content SHA-256, вложенные фотографические EXIF-поля, dimensions, `decode_status` и provenance превью. Для RAW provenance различает embedded JPEG/bitmap и postprocess fallback. SQLite используется только для пофайлового checkpoint/cache; JSONL записывается как целостный обменный и аудиторский snapshot, а не переписывается после каждого кадра.

`groups.json` хранит неплоские отношения: byte duplicates, RAW/JPEG companions, bursts, near duplicates и защищённые HDR/panorama/focus-stack комплекты. Один asset может участвовать в нескольких отношениях. `selection-items.json` схлопывает только подтверждённые связи: RAW+JPEG становится одним capture-item, а защищённый комплект — одним composite-item со всеми `required_asset_ids`. Слабые или конфликтующие признаки остаются `uncertain` и не объединяют пункты отбора.

`grouping-report.json` показывает число кандидатов, типы групп и `review_group_ids`. Значение `real_photo_validation=unavailable` честно сохраняется, пока нет разрешённого пилотного набора; синтетические fixtures не объявляются доказательством совместимости конкретной камеры.

`assessments.jsonl` хранит численные сигналы по каждому asset отдельно от visual evidence. Локальная дисперсия Лапласиана, яркость и высокочастотная энергия являются только baseline-сигналами: low/high key, grain, soft focus и motion blur остаются контекстными. Этап `assess` создаёт только proposal-решения `keep`, `review` или `unassessed`; `automatic_rejects` в отчёте равен нулю. Затем `curate` обновляет `decisions.jsonl` item-level уровнями `top`, `select`, `reserve` или `none`, сохраняя пользовательские overrides. Оба этапа используют marker `publishing` → `complete`, поэтому прерванная публикация не читается как согласованный результат. Общие исключения описаны в [`resources/artistic-intent.md`](resources/artistic-intent.md), жанровые — в [`resources/genres/`](resources/genres/).

### Валидация полного run-контракта

Eval-harness проверяет артефакты последующих этапов. `prepare` создаёт `assets.jsonl`, а `group` добавляет `groups.json` и `selection-items.json`; остальные файлы появятся в задачах оценки и отбора:

```text
run/
├── assets.jsonl
├── groups.json
├── selection-items.json
├── assessments.jsonl
├── decisions.jsonl
├── selection.json
└── delivery-plan.json      # необязателен до этапа delivery
```

Python API:

```python
from pathlib import Path

from evals.runner import grade_artifacts, validate_run

errors = validate_run(Path("run"))

grade = grade_artifacts(
    {
        "case_id": "reference-contracts",
        "brief": {"top_k": 1},
        "gold_good_item_ids": ["item-1"],
    },
    Path("run"),
)
```

`validate_run()` всегда возвращает список ошибок вместо аварийного завершения на некорректных артефактах. `grade_artifacts()` возвращает общий результат, отдельные deterministic checks и объяснения ошибок.

## Данные и безопасность

- Реальные фотографии не входят в репозиторий.
- `prepare` читает оригиналы и проверяет неизменность content hash до и после render; изменившийся во время запуска файл получает честный `degraded`, а не ложное готовое превью.
- Служебные пути output проверяются на выход за корень и небезопасные симлинки до любых изменений.
- Группировка использует индекс по камере/времени с ограниченным числом соседей, а не полный попарный перебор; совпадение имени без согласованных EXIF-свидетельств не создаёт RAW/JPEG-пару.
- HDR, panorama и focus stack являются защищёнными наборами: неоднозначное пересечение переводится в `uncertain`, а не приводит к потере обязательных исходников.
- Маленькое превью, неподдерживаемый RAW, возможный смаз, шум и крайняя экспозиция дают uncertainty/review, а не безусловный reject.
- Визуальные утверждения принимаются только вместе с известными `observed_artifact_ids`, полным набором критериев и совпадающим fingerprint входов; невалидный, устаревший или неполный ответ не становится успешным результатом.
- Шесть плановых pilot-съёмок явно отмечены `real-data-unavailable` в [`evals/dataset-manifest.json`](evals/dataset-manifest.json).
- Для файловых проверок используется созданный в репозитории CC0 PPM-fixture с зафиксированным SHA-256.
- Автоматическое удаление фотографий не входит в проект.
- MVP рассматривает reject как рекомендацию; proposed reject не становится флагом редактора без явного разрешения.
- Silverloupe не изменяет `.lrcat` напрямую и не должен перезаписывать существующую XMP/ACR-обработку с нуля.

## Для агентов-разработчиков

### Начало работы

1. Прочитайте этот README и актуальную задачу в GitHub Issues.
2. Прочитайте раздел задачи в [implementation plan](docs/superpowers/plans/2026-10-22-silverloupe.md).
3. Для контрактов и поведения сверяйтесь с [design spec](docs/superpowers/specs/2026-10-22-silverloupe-design.md) и [eval-программой](docs/evals.md).
4. Запустите `uv sync --locked` и `uv run --locked pytest -q` до изменений.
5. Реализуйте только выбранную задачу, соблюдая RED → GREEN для кода.
6. Перед завершением повторите полный тестовый прогон и `silverloupe doctor`.

### Источники истины

При расхождениях используйте следующий приоритет:

1. требования текущего GitHub Issue;
2. design spec и глобальные ограничения;
3. implementation plan;
4. eval-программа;
5. этот README как операционная сводка.

README обязан отражать фактическое состояние репозитория, но не заменяет подробные спецификации.

### Неподвижные ограничения

- Production-поставка не содержит eval-harness, тестовые данные и dev-зависимости.
- Runtime не импортирует `evals`; `TrialResult` остаётся dev-only контрактом.
- Все команды разработки выполняются через `uv run --locked`.
- Не добавляйте ложный fallback: отсутствие RAW, vision или editor capability должно давать `degraded`, `unavailable`, `partial` либо `failed`.
- Не коммитьте реальные фотографии, приватные данные, generated reports и run-output.
- Не объявляйте поддержку редактора, модели, ОС или формата без проверенной compatibility fixture.
- Manual overrides имеют приоритет и должны сохраняться при повторном прогоне.

## Карта репозитория

| Путь | Назначение |
|---|---|
| `src/silverloupe/contracts.py` | Runtime-контракты и инварианты |
| `src/silverloupe/cli.py` | CLI и `silverloupe doctor` |
| `src/silverloupe/inventory.py` | Read-only scan, hashing и orchestration `prepare` |
| `src/silverloupe/previews.py` | Orientation-aware previews и contact sheets |
| `src/silverloupe/state.py` | SQLite checkpoint, session и status |
| `src/silverloupe/grouping.py` | Индекс кандидатов, группы и `SelectionItem` |
| `src/silverloupe/comparisons.py` | Бок-о-бок comparison batches |
| `src/silverloupe/signals.py` | Консервативные численные сигналы по превью |
| `src/silverloupe/triage.py` | Assessment, review-only decisions и артефакты CLI |
| `src/silverloupe/curation.py` | Жанровые оценки, constrained selection и публикация `selects` / `top` |
| `src/silverloupe/vision/` | Контракты и валидация host-vision exchange |
| `resources/artistic-intent.md` | Общие художественные исключения для triage |
| `resources/profiles/` | Версионированные веса и diversity policy жанров |
| `resources/genres/` | Критерии, исключения, coverage и отрицательные примеры жанров |
| `evals/runner.py` | Dev-only validation и deterministic graders |
| `evals/contracts.py` | Dev-only `TrialResult` |
| `evals/cases/` | Версионированные eval cases |
| `evals/rubrics/` | Описания deterministic checks |
| `tests/contracts/` | Контрактные тесты моделей и CLI |
| `tests/unit/` | Unit-тесты graders, inventory, grouping и triage |
| `tests/integration/` | Resume, cache, RAW/EXIF и безопасность подготовки |
| `docs/superpowers/specs/` | Архитектура и дизайн |
| `docs/superpowers/plans/` | Пошаговый implementation plan |
| `docs/evals.md` | Данные, метрики, baselines и release gates |
| `docs/distribution.md` | Production/install поставка |
| `docs/requirements.md` | Runtime, модели, ресурсы и optional dependencies |

## Как сохранять README актуальным

Изменение не считается завершённым, если оно меняет пользовательское или агентское поведение, но не обновляет соответствующий раздел README.

Перед каждым merge необходимо проверить:

- команды установки и запуска копируются и выполняются без ручных поправок;
- таблица статуса не выдаёт планируемую функцию за готовую;
- новые CLI-команды, артефакты и обязательные зависимости документированы;
- ограничения, degraded-режимы и опасные операции описаны явно;
- карта репозитория и ссылки не содержат несуществующих путей;
- README остаётся краткой точкой входа, а детали не дублируют спецификации целиком.

## Документация

Рекомендуемый порядок чтения:

1. [Архитектура и состав коллекции](docs/superpowers/specs/2026-10-22-silverloupe-design.md)
2. [План разработки](docs/superpowers/plans/2026-10-22-silverloupe.md)
3. [Программа evals](docs/evals.md)
4. [Поставка и установщик](docs/distribution.md)
5. [HTML-review следующей итерации](docs/superpowers/specs/2026-10-22-silverloupe-review-ui-design.md)
6. [Требования к моделям и ресурсам](docs/requirements.md)
7. [Исследование и первичные источники](docs/research.md)

---

Основной репозиторий: [github.com/sendelufa/silverloupe](https://github.com/sendelufa/silverloupe)
