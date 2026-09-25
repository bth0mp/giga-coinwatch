FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COINWATCH_DATA_DIR=/data

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -c requirements.lock . \
    && groupadd --system --gid 10001 coinwatch \
    && useradd --system --uid 10001 --gid coinwatch --home-dir /data coinwatch \
    && mkdir -p /data \
    && chown coinwatch:coinwatch /data

USER coinwatch
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=3).close()" || exit 1

CMD ["python", "-m", "coinwatch", "run", "--host", "0.0.0.0", "--port", "8000"]
