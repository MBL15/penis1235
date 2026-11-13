# Образ для docker compose: зависимости ставятся из PyPI, дополнительных файлов (wheels/) не нужно.
# Для серверов без доступа к PyPI есть deploy/Dockerfile.offline (колёса из каталога wheels/), его использует deploy/run.sh.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/afisha
COPY requirements.txt .
# Процесс работает не от root. Каталог /data принадлежит ему: новый именованный том наследует этого владельца.
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin afisha \
    && mkdir /data && chown 1000:1000 /data
COPY app app
COPY data/sample_events.json data/sample_events.json
COPY deploy/entrypoint.sh entrypoint.sh
USER 1000
EXPOSE 8000
ENTRYPOINT ["sh", "/srv/afisha/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header", "--no-access-log"]
