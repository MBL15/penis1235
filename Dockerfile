FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/afisha
COPY wheels /wheels
COPY requirements.txt .
# PyPI с сервера недоступен: ставим из заранее скачанных колёс. Отдельный пользователь без прав: процесс не root.
RUN pip install --no-cache-dir --no-index --find-links /wheels -r requirements.txt && rm -rf /wheels \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin afisha
COPY app app
USER 1000
CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--no-server-header"]
