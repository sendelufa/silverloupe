# Silverloupe

Silverloupe помогает подготовить, сравнить и отобрать фотографии, а затем
перенести подтверждённую разметку в поддерживаемый редактор.

## Быстрый старт

Production-поставка рассчитана на macOS Apple Silicon. Установите `uv`,
распакуйте release-архив и из его корня выполните:

```bash
uv python install 3.12
uv run --python 3.12 --no-project \
  --with-requirements runtime/requirements.lock \
  python install.py --help
```

Выберите хост (`codex`, `claude-code` или оба), редактор и режим интеграции.
Режим `native` требует доступной и проверенной интеграции приложения. Режим
`file` выбирается явно и не включается автоматически. Без доступного writer
Silverloupe остаётся в безопасном режиме `report-only`.

Codex-скиллы регистрируются в `.agents/skills`, Claude Code — в
`.claude/skills`. При `--scope user` это каталоги домашней директории, при
`--scope project` — каталоги внутри выбранного `install-root`.

Архив содержит только шесть рабочих скиллов и production runtime. Фото-evals,
тесты и инструменты разработчика в пользовательскую поставку не входят.

## Режимы и возможности

- `host-vision` использует визуальные возможности выбранного Codex/Claude-хоста
  и не скачивает локальные веса модели.
- `local-signals` формирует только технический partial report.
- RAW без проверенного renderer редактора требует совместимый binary wheel
  `rawpy`; установщик не предлагает сборку C/C++ зависимости на машине
  фотографа. Для явно выбранного `--raw` bootstrap использует
  `runtime/requirements-raw.lock`.
- При недоступном MCP/native backend выбранный редактор остаётся
  `report-only`. Переход к XMP выполняется только явным выбором `file`.

Первая поддерживаемая платформа — macOS на Apple Silicon с Python 3.12.
Целевой компьютер имеет 16 ГБ RAM; стартовый soft budget Python — 3 ГБ RSS.
Для runtime/обновления резервируется до 5 ГБ, а рабочий кэш съёмки задаётся
отдельной квотой. Эти значения — release-бюджеты, не обещание скорости для
любой камеры и съёмки.

## Повторная установка, обновление и удаление

Повторная установка той же версии идемпотентна. Обновление сначала проверяет
и подготавливает новую версию, затем переключает `active-version`; ошибка до
переключения сохраняет прежнюю установку. Смена редактора применяется к новым
сессиям и не переписывает snapshot или delivery-plan существующей сессии.

Удаление читает ownership manifest и удаляет только неизменённые файлы
Silverloupe. Чужие скиллы, фотографии, результаты и каталоги сессий остаются.
Изменённый owned-файл также сохраняется и перечисляется в результате удаления.

Установленный launcher предоставляет управление без dev-checkout:

```bash
/path/to/install-root/bin/silverloupe doctor
/path/to/install-root/bin/silverloupe configure --editor lightroom-classic --mode file
/path/to/install-root/bin/silverloupe update /path/to/silverloupe-VERSION.tar.gz
/path/to/install-root/bin/silverloupe uninstall
```

`doctor` показывает выбранные host/editor/model возможности. Если конкретный
Adobe/XMP маршрут ещё не подтверждён release-матрицей, выбранный `file` режим
остаётся `report-only`, а не объявляется готовым writer.

Публикация install-репозитория или GitHub release — отдельное release-действие:
сборщик только создаёт проверяемый архив и не отправляет его во внешний сервис.
