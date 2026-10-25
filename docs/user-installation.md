# Установка Silverloupe

Silverloupe устанавливается через пошаговый мастер в Terminal. Пользователю не
нужно вручную скачивать архив, выбирать каталог, создавать virtualenv или искать
подходящую версию Python.

## Одна команда

После публикации первого production-релиза команда будет такой:

```bash
curl -fsSL https://github.com/sendelufa/silverloupe/releases/latest/download/install.sh | sh
```

**Сейчас этот публичный URL ещё не активирован.** Bootstrap и TUI подготовлены и
проверяются локально; исходный bootstrap находится в `install.sh` в корне
репозитория. Публикация GitHub Release будет отдельным шагом.

## Что сделает мастер

Сначала bootstrap выполняет безопасные проверки, ничего не меняя на компьютере:

1. Проверяет, что используется macOS на Apple Silicon.
2. Ищет команды `codex` и `claude`.
3. Если ни один агент не найден, останавливается и показывает официальные ссылки.
4. Скачивает production-релиз и проверяет его SHA-256 до распаковки.
5. Предлагает установить `uv` и управляемый Python 3.12, если они отсутствуют.

Codex и Claude Code мастер никогда не устанавливает и не обновляет.

После проверок открывается пошаговый интерфейс. Он задаёт только вопросы, которые
действительно меняют установку:

- какой агент использовать, если найдены и Codex, и Claude Code;
- какой фоторедактор использовать, если найдены совместимые приложения;
- подтверждение перед установкой.

Каталог, cache quota, число workers, Python environment и остальные технические
параметры выбираются автоматически. По умолчанию Silverloupe устанавливается в:

```text
~/Library/Application Support/Silverloupe
```

Codex-скиллы регистрируются в `~/.agents/skills`, Claude Code — в
`~/.claude/skills`.

## Если фоторедактора нет

Это не блокирует работу. Silverloupe сможет:

- подготовить фотографии и превью;
- сравнить похожие кадры;
- сформировать selects и top;
- создать локальный HTML-отчёт.

Перенос рейтингов, flags и labels останется выключенным. Если приложение найдено,
но его интеграция не подтверждена runtime-проверкой, мастер также использует
безопасный `report-only`, а не притворяется, что запись метаданных доступна.

LightCraft запрашивает native-интеграцию и автоматически остаётся `report-only`,
если выбранный агент не видит нужные MCP capabilities. Найденные Adobe-приложения
в этом релизе записываются как выбранный редактор, но остаются `report-only` до
реального application round-trip.

## После установки

Мастер сам запускает `doctor` и показывает понятный итог: готовые агенты,
возможности редактора, RAW и ограничения. Затем откройте выбранный агент и попросите
его использовать Silverloupe для папки с фотографиями.

Команды диагностики и управления остаются доступны напрямую:

```bash
~/Library/Application\ Support/Silverloupe/bin/silverloupe doctor
~/Library/Application\ Support/Silverloupe/bin/silverloupe configure \
  --editor lightcraft --mode native
~/Library/Application\ Support/Silverloupe/bin/silverloupe uninstall
```

Повторный запуск TUI обнаруживает существующую установку и предлагает обновление,
перенастройку или безопасное удаление. Uninstall удаляет только неизменённые файлы
из ownership manifest; пользовательские и изменённые файлы сохраняются.

## Требования первой версии

- macOS;
- Apple Silicon (`arm64`);
- установленный CLI Codex или Claude Code;
- интерактивный Terminal;
- около 5 ГБ свободного места для runtime и обновления.

Intel Mac, Linux и Windows пока не заявлены как поддерживаемые платформы.

## Automation и локальная квалификация

Этот раздел нужен разработчикам и CI, а не обычному пользователю.

Собрать архив, checksum и bootstrap без публикации:

```bash
uv run --locked python -c '
from pathlib import Path
from tools.build_collection import build_release_assets
print(build_release_assets(Path.cwd(), Path("dist/release"), "0.1.0"))
'
```

Запустить локально собранный bootstrap:

```bash
SILVERLOUPE_RELEASE_URL="file://$PWD/dist/release/silverloupe-0.1.0.tar.gz" \
SILVERLOUPE_RELEASE_SHA256_URL="file://$PWD/dist/release/silverloupe-0.1.0.tar.gz.sha256" \
sh ./install.sh
```

Для полностью неинтерактивной установки из уже распакованного проверенного релиза:

```bash
uv python install 3.12
uv run --python 3.12 --no-project \
  --with-requirements runtime/requirements.lock \
  python install.py --non-interactive \
  --host codex \
  --editor report-only \
  --mode report-only \
  --install-root "$HOME/Library/Application Support/Silverloupe"
```

Флаги `--host` можно повторить для Codex и Claude Code. Optional RAW decoder
включается только явным `--raw`; обычный мастер не задаёт этот преждевременный
вопрос и оставляет extra выключенным.
