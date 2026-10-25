# Silverloupe compatibility

Проверено 25 октября 2026 года. Эта таблица отделяет наличие кода и
contract-fixtures от реальной совместимости конкретного приложения, модели или
камеры. `not-tested` означает именно отсутствие подтверждающего round-trip, а не
скрытую поддержку.

## Пользовательская платформа

| Компонент | Статус | Ограничение |
|---|---|---|
| macOS на Apple Silicon (`arm64`) | supported | Единственная платформа, которую принимает `install.sh` |
| Intel Mac | unsupported | Установщик останавливается до сети и изменений |
| Linux / Windows | unsupported | Не заявлены для первой пользовательской версии |
| Python 3.12 | supported | Изолированный runtime устанавливается и управляется через `uv` |
| Codex CLI | harness-ready | На машине найден `codex-cli 0.153.4`; release live-suite ещё не выполнен |
| Claude Code | harness-ready, live unavailable | На машине найден CLI 2.1.296; последний сохранённый smoke завершился ошибкой аутентификации |

## Форматы и RAW

- JPEG/PNG/TIFF и разрешённые preview-fixtures проходят deterministic core tests.
- RAW decoder `rawpy` является optional extra и не устанавливается молча.
- Поддержка конкретной камеры не выводится из наличия wheel или LibRaw: она
  подтверждается отдельным fixture/readback.
- Неизвестный или повреждённый RAW сохраняется и получает
  `decode_error`/`unassessed`, а не выдуманную художественную оценку.

## Редакторы

| Редактор/маршрут | Доказательство | Текущий режим |
|---|---|---|
| Lightroom Classic, XMP sidecar | Core merge/apply/readback/rollback contract-tested; 10 application/format combinations в matrix имеют `not-tested` | `report-only` до application round-trip |
| Lightroom Cloud | Нет проверенного файлового/native writer | `report-only` |
| Photoshop / Bridge / Camera Raw | Core XMP сохранность contract-tested; приложение/форматы не проверены | `report-only` |
| LightCraft native | Fake-server contracts и explicit-ID host-mediated flow проверены; полный release matrix ещё не закрыт | `native` только при реально доступной capability, иначе `report-only` |
| Статический HTML report | Deterministic integration tests | supported |

Источник машинно-читаемых статусов Adobe:
[`evals/compatibility-matrix.json`](../evals/compatibility-matrix.json). Наличие
приложения на диске не переводит строку в `verified`.

## Что ещё нужно для расширения support matrix

Для каждой новой строки требуются версия приложения и macOS, архитектура, формат,
fixture SHA-256, запись, чтение приложением, readback, сохранность develop/ACR,
flags и пользовательских color labels. До этого installer и delivery остаются
fail-closed.
