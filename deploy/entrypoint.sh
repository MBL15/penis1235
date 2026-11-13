#!/bin/sh
# Перед запуском: создаёт таблицы и, если не отключено, подгружает набор реальных событий data/sample_events.json
# (повторная загрузка дублей не создаёт). Отключить: LOAD_SAMPLE_DATA=0.
set -e
if [ "${LOAD_SAMPLE_DATA:-1}" = "1" ]; then
  python -m app.ingest data/sample_events.json
fi
exec "$@"
