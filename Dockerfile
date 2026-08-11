# syntax=docker/dockerfile:1.7

# Exact patch tag is the reproducible default; production may pass a digest-pinned
# replacement with --build-arg PYTHON_IMAGE=python@sha256:...
ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:${PATH}

# Install CPU torch first so sentence-transformers cannot pull a CUDA stack.
RUN python -m pip install 'pip==25.1.1' \
    && python -m pip install --index-url https://download.pytorch.org/whl/cpu \
       'torch==2.7.1+cpu'

COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install .


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
    HF_HOME=/opt/hf-cache \
    XDG_CACHE_HOME=/opt/hf-cache/xdg \
    MPLCONFIGDIR=/tmp/matplotlib \
    APP_SOURCE_SHA=${APP_SOURCE_SHA} \
    PROMPT_VERSION=${PROMPT_VERSION} \
    PROMPT_SCHEMA_SHA=${PROMPT_SCHEMA_SHA}

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
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
