# Silverloupe

> Every frame deserves a closer look.

Silverloupe — проект проверяемого ассистированного отбора фотографий для Codex и Claude Code. Цель — помочь фотографу разобрать съёмку, сохранить значимые кадры и защищённые комплекты, сформировать рабочий отбор и топ, а затем безопасно перенести подтверждённую разметку в Lightroom, Photoshop / Camera Raw или LightCraft.

## Текущий статус

**Реализованы контракты данных, deterministic eval-harness, диагностика окружения, read-only подготовка съёмки и консервативная группировка серий/композитов.** Техническая оценка и сам художественный отбор фотографий пока не реализованы.

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
| Техническая и художественная оценка | Запланировано |
| Отчёт, ручные исправления и перенос в редакторы | Запланировано |
| Production-набор из шести скиллов и установщик | Запланировано |

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

Пересекающиеся защищённые composite-группы команда отклонит: сначала конфликт нужно разрешить в `groups.json` или перевести связь в `uncertain`.

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
├── previews/              # JPEG + provenance sidecars
└── contact-sheets/
```

`assets.jsonl` содержит относительный путь, отдельные `asset_id` и content SHA-256, вложенные фотографические EXIF-поля, dimensions, `decode_status` и provenance превью. Для RAW provenance различает embedded JPEG/bitmap и postprocess fallback. SQLite используется только для пофайлового checkpoint/cache; JSONL записывается как целостный обменный и аудиторский snapshot, а не переписывается после каждого кадра.

`groups.json` хранит неплоские отношения: byte duplicates, RAW/JPEG companions, bursts, near duplicates и защищённые HDR/panorama/focus-stack комплекты. Один asset может участвовать в нескольких отношениях. `selection-items.json` схлопывает только подтверждённые связи: RAW+JPEG становится одним capture-item, а защищённый комплект — одним composite-item со всеми `required_asset_ids`. Слабые или конфликтующие признаки остаются `uncertain` и не объединяют пункты отбора.

`grouping-report.json` показывает число кандидатов, типы групп и `review_group_ids`. Значение `real_photo_validation=unavailable` честно сохраняется, пока нет разрешённого пилотного набора; синтетические fixtures не объявляются доказательством совместимости конкретной камеры.

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
| `evals/runner.py` | Dev-only validation и deterministic graders |
| `evals/contracts.py` | Dev-only `TrialResult` |
| `evals/cases/` | Версионированные eval cases |
| `evals/rubrics/` | Описания deterministic checks |
| `tests/contracts/` | Контрактные тесты моделей и CLI |
| `tests/unit/` | Unit-тесты graders, inventory и grouping |
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
