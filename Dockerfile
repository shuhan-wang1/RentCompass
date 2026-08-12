# syntax=docker/dockerfile:1.7

# Official Python 3.12.11 slim-bookworm multi-arch index, resolved 2026-08-12.
# Production still passes the same contract through DEPLOY_PYTHON_IMAGE so an
# operator can advance it deliberately without editing a release commit.
ARG PYTHON_IMAGE=python@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:${PATH}

COPY constraints-production.txt requirements-bootstrap.lock requirements-production.lock ./

# Every registry artifact is hash-locked. requirements-production.lock pins the
# CPU-only torch local version, so the extra index cannot substitute an unreviewed file.
RUN python -m pip install --require-hashes -r requirements-bootstrap.lock \
    && python -m pip install --require-hashes -r requirements-production.lock

COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-deps --no-build-isolation . \
    && python -m pip check


FROM ${PYTHON_IMAGE} AS runtime

ARG APP_SOURCE_SHA=unknown
ARG PROMPT_VERSION=unknown
ARG PROMPT_SCHEMA_SHA=unknown

LABEL org.opencontainers.image.title="RentCompass agent" \
      org.opencontainers.image.revision="${APP_SOURCE_SHA}" \
      org.opencontainers.image.source="https://github.com/rentcompass/uk_rent_recommendation" \
      uk.rentcompass.prompt.version="${PROMPT_VERSION}" \
      uk.rentcompass.prompt.schema-sha="${PROMPT_SCHEMA_SHA}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:${PATH} \
    APP_PROJECT_ROOT=/app \
    HF_HOME=/opt/hf-cache \
    XDG_CACHE_HOME=/opt/hf-cache/xdg \
    MPLCONFIGDIR=/tmp/matplotlib \
    APP_SOURCE_SHA=${APP_SOURCE_SHA} \
    PROMPT_VERSION=${PROMPT_VERSION} \
    PROMPT_SCHEMA_SHA=${PROMPT_SCHEMA_SHA}

RUN apt-get update \
    && apt-get install -y --no-install-recommends 'libgomp1=12.2.0-14+deb12u1' \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/.runtime /opt/hf-cache/xdg \
    && chown -R app:app /app /opt/hf-cache

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app app ./app

USER app
EXPOSE 5001

CMD ["python", "-m", "uvicorn", "uk_rent_agent.web.asgi:create_asgi_app", \
     "--factory", "--host", "0.0.0.0", "--port", "5001"]
