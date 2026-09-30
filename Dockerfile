# fontem-currency: daily exchange rates + EUR conversion.
#
# Built like the other Python services: a venv made in Chainguard's -dev
# image, copied into the distroless runtime (no shell, no package manager),
# pip removed. The bases are pinned by digest (Renovate keeps them current).
# Dependencies come from pyproject.toml; this file used to pin its own older
# versions (fastapi 0.118, uvicorn 0.37), so the image never ran what
# pyproject declared.
FROM cgr.void42.internal/chainguard/python:latest-dev@sha256:5eef76bbb8d9f815317da126075705202b8ca5c2a151d723e7ecdf0373d9d861 AS build
USER root
ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal
COPY void42-ca.crt /tmp/void42-ca.crt
RUN cat /tmp/void42-ca.crt >> /etc/ssl/certs/ca-certificates.crt
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
WORKDIR /build
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . \
 && pip uninstall -y pip \
 && mkdir -p /out/srv/currency-data

FROM cgr.void42.internal/chainguard/python:latest@sha256:38ba1cbf71702bacc5f5be22ea41e3d4ad1bfb2565413b0caf4bafde38e831f2
WORKDIR /app
COPY --from=build /venv /venv
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/srv/currency-data /srv/currency-data
ENV PATH="/venv/bin:$PATH" \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CURRENCY_DATA_DIR=/srv/currency-data
# The chart runs the pods as uid 999, the owner of the existing rates on NFS.
USER 65532
EXPOSE 8080
# The package is installed in the venv (pyproject: packages "src*"), so
# `python -m src.api.app` and the loader CronJob's `python -m
# src.loader.load` import it from there.
ENTRYPOINT ["/venv/bin/python", "-m", "uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
