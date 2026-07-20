FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --requirement requirements.txt
COPY app app
COPY templates templates
COPY static static

RUN groupadd --gid 1000 yandexmusic2feed \
  && useradd --uid 1000 --gid yandexmusic2feed --create-home yandexmusic2feed \
  && mkdir --parents /data /podcasts \
  && chown yandexmusic2feed:yandexmusic2feed /data /podcasts

USER yandexmusic2feed

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
