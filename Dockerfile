FROM python:3.13-slim

COPY void42-ca.crt /usr/local/share/ca-certificates/void42-ca.crt
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    update-ca-certificates && rm -rf /var/lib/apt/lists/*

ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CURRENCY_DATA_DIR=/srv/currency-data

RUN groupadd -r appuser && useradd -r -g appuser appuser && \
    mkdir -p /srv/currency-data && chown appuser:appuser /srv/currency-data

WORKDIR /app

# Dependencies first (better layer caching). pyproject is the source of
# truth; we install in non-editable mode so the package itself ends up
# in site-packages and the loader+api can both `python -m src.*`.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi==0.118.2 uvicorn==0.37.0 httpx==0.28.1 pydantic==2.12.3

COPY src/ ./src/

USER appuser

# The API serves on 8080. The loader is invoked as a separate
# entrypoint by the cronjob: `python -m src.loader.load`.
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
