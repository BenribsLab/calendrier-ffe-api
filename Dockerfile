FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Paris \
    CAL_DATA_DIR=/data

# tzdata : l'analyse quotidienne du CDE se cale sur l'heure de Paris
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

RUN useradd --system --uid 1000 --home /app calendrier \
    && mkdir -p /data && chown calendrier:calendrier /data
USER calendrier
VOLUME ["/data"]

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)"

# Un seul worker : les caches et la tâche quotidienne CDE 91 vivent dans le processus.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers", "--forwarded-allow-ips", "*"]
