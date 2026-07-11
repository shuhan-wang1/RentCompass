# syntax=docker/dockerfile:1
#
# Image for the UK-rent agent web app (ASGI/uvicorn on :5001).
#
# The installable package lives under src/ (uk_rent_agent). The domain tools and
# RAG code live under app/ and are added to sys.path at runtime by
# uk_rent_agent.web.app — so both trees must be present in the image, but only
# the src package is pip-installed.
#
# Runtime data (chroma indexes, .env, .runtime sqlite, scraped/cache data) is NOT
# baked in — it is bind-mounted from the host in docker-compose.yml so the
# container shares the same pre-built indexes and secrets as the host.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # HuggingFace / sentence-transformers model cache lands on a named volume.
    HF_HOME=/opt/hf-cache

WORKDIR /app

# build-essential/gcc cover any dependency that falls back to building from sdist
# (most wheels are prebuilt manylinux). curl is used by the container healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# --- CPU-only PyTorch (pinned first so transitive deps don't pull CUDA) -------
# sentence-transformers depends on torch; by default pip installs the CUDA build
# (~5GB of nvidia/* + triton wheels this CPU deployment never uses). Installing
# the CPU wheel first satisfies that dependency so the package install below
# won't fetch the GPU stack.
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch

# --- Dependency layer (cached unless pyproject changes) ----------------------
# Copy just the metadata + package source needed to resolve and install deps.
COPY pyproject.toml ./
COPY src ./src
RUN pip install -e .

# --- Application code --------------------------------------------------------
# The domain/RAG code the app imports at runtime via sys.path insertion.
COPY app ./app

EXPOSE 5001

# Uvicorn factory, same entrypoint the host currently runs, bound to all
# interfaces so the published port is reachable.
CMD ["python", "-m", "uvicorn", "uk_rent_agent.web.asgi:create_asgi_app", \
     "--factory", "--host", "0.0.0.0", "--port", "5001"]
