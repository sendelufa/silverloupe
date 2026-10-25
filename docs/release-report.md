# Silverloupe 0.1.0 — локальный release report

Дата проверки: 25 октября 2026 года. Публикация GitHub Release не выполнялась.

## Решение

**Локальный артефакт пригоден только для режима `recommendation-only`.**
Auto-reject выключен. Публичный выпуск остаётся отложенным до live-evals и решения
владельца проекта о публикации.

## Точный артефакт

| Поле | Значение |
|---|---|
| Version | `0.1.0` |
| Archive | `dist/issue-10-final/silverloupe-0.1.0.tar.gz` |
| SHA-256 | `6e952f11cc252cc881902f6a086cbb50dd836bed89276e745f9edfb28f2db3d9` |
| Size | 108943 bytes |
| Qualification | `recommendation-only` |
| Bootstrap source | корневой `install.sh` |

`dist/` является локальным git-ignored output. Эти значения описывают конкретную
сборку текущего checkout; после любого изменения production payload артефакт и
SHA необходимо построить заново.

## Подтверждено

- production archive строится по allowlist и не содержит `evals`, tests, gold,
  traces, внутренних планов или dev dependencies;
- manifest ownership и SHA-256 каждого payload-файла замкнуты внутри архива;
- release qualification проверяет точный archive hash, version/root, unsafe
  members, forbidden material и checksum mismatch;
- корневой `install.sh` копируется в release assets byte-for-byte и остаётся
  отдельным bootstrap, не подменяющим проверенный runtime;
- метрики корректно возвращают unavailable для пустых знаменателей и считают
  rejection, nDCG@K, routing, latency и trial reliability на известных примерах;
- Codex/Claude adapters используют проверенные non-interactive CLI flags, чистый
  trial directory и structured observable result; malformed output не становится
  PASS;
- release trials устанавливают точный archive в project-scoped workspace перед
  запуском host CLI.

## Недоступные доказательства и blockers

1. В manifest шесть pilot-съёмок имеют `real-data-unavailable`; разрешённые
   реальные wedding/architecture/landscape datasets не предоставлены.
2. Все 10 Adobe application/format combinations остаются `not-tested`.
3. Полный live release run Codex + Claude Code не выполнялся в этой итерации.
4. Cold/warm p50/p95, peak RSS/disk, 1000/5000-file performance и 8/16/32 GB
   matrix не измерены на реальных съёмках.
5. Набора из 30 съёмок в любом случае недостаточно для false-reject upper bound
   ≤0.5%; до отдельного мощного протокола auto-reject запрещён.

## Воспроизводимые команды

Сборка локальных assets:

```bash
uv run --locked python -c '
from pathlib import Path
from tools.build_collection import build_release_assets
print(build_release_assets(Path.cwd(), Path("dist/issue-10-final"), "0.1.0"))
'
```

Полный host runner после предоставления данных и бюджета:

```bash
uv run --locked python -m evals.runner \
  --suite release \
  --hosts codex,claude \
  --trials 5 \
  --artifact dist/issue-10-final/silverloupe-0.1.0.tar.gz \
  --output runs/release-0.1.0
```

Текущий встроенный `release` scope выполняет routing/disclosure cases и честно
оставляет фото/editor/performance gates недоступными. Реальные фото-cases можно
передать через `--cases`; каждый trial получает отдельную установку точного
артефакта.

## Следующее release-действие

Предоставить согласованные реальные съёмки и отдельный бюджет на live trials,
запустить host/photo/editor/performance matrix, обновить этот отчёт фактическими
counts/CI и только затем принять решение о GitHub Release.
