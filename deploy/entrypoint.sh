#!/bin/sh
# Перед запуском: создаёт таблицы и, если не отключено, подгружает набор реальных событий data/sample_events.json
# (повторная загрузка дублей не создаёт). Отключить: LOAD_SAMPLE_DATA=0.
# После загрузки карточки проходят проверку без сети (дата, полнота, дубли): у каждой появляются verified_at и confidence
# (ТЗ §6.5). Ссылки на источники проверяет планировщик или `python -m app.verify run`. Сбой проверки запуск не останавливает.
set -e
if [ "${LOAD_SAMPLE_DATA:-1}" = "1" ]; then
  python -m app.ingest data/sample_events.json
  python -m app.verify run --no-links --keep --limit 1000 || echo "Проверка карточек не выполнена, сервис запускается без неё" >&2
fi
exec "$@"
