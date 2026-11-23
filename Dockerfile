# syntax=docker/dockerfile:1.7
# AURA image: WhatsApp server + dashboard + both trained models (no secrets, no data).
# Build:  docker build -t aura:latest .
# Run:    docker compose up -d        (see README.md)
#
# Two stages: "builder" installs the Python libraries into /opt/venv; the final image copies only that
# folder, so pip, its caches and build leftovers never reach the image. Layers are ordered from
# rarely-changing to often-changing (torch -> libraries -> models -> code): editing the code rebuilds
# in seconds and never re-downloads torch or re-copies the models.
#
# Slow internet? Reuse the libraries of an AURA image you already have instead of downloading them
# (only when requirements.txt hasn't changed since that image was built):
#   docker tag aura:latest aura-deps
#   docker build --build-arg LIBS_FROM=aura-deps -t aura:latest .

# Base image pinned to an exact version: builds are reproducible and the libraries' cache stays valid.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG LIBS_FROM=builder

# ------------------------------------------------------------------ 1. builder
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH
RUN python -m venv /opt/venv

# CPU-only torch in its own layer: the biggest download (~200 MB), and it almost never changes.
# The pip cache is a BuildKit cache mount: it speeds up rebuilds but is not part of the image.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Files only needed to compile extensions or run library test suites (not used by AURA).
RUN SITE=/opt/venv/lib/python3.12/site-packages \
 && rm -rf "$SITE/torch/include" "$SITE/torch/share" \
           "$SITE/pandas/tests" "$SITE/pyarrow/include" "$SITE/pyarrow/tests" \
 && pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true

# Where /opt/venv comes from: the builder above (default), or an existing AURA image (LIBS_FROM).
FROM ${LIBS_FROM} AS libs

# ------------------------------------------------------------------ 2. runtime
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    TZ=Asia/Karachi \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    AURA_DB=/app/storage/aura.db \
    INTENT_MODEL_DIR=/app/models/intent_model_v2_fp16 \
    ENTITY_MODEL_DIR=/app/models/entity_model_fp16 \
    MODEL_DTYPE=float16

# Pakistan time (TZ above) uses the image's own tzdata; fail the build if a future base image drops it.
# Non-root user; /app/storage holds the database (a Docker volume).
RUN test -f /usr/share/zoneinfo/Asia/Karachi \
 && useradd --create-home --uid 1000 aura \
 && mkdir -p /app/storage \
 && chown aura:aura /app/storage

WORKDIR /app
COPY --from=libs /opt/venv /opt/venv
# Cloudflare tunnel program (a single file, ~40 MB): AURA runs it to get a public address for WhatsApp.
COPY --from=cloudflare/cloudflared:2026.11.0 /usr/local/bin/cloudflared /usr/local/bin/cloudflared

# Trained models in half precision (2 x 0.55 GB, made by training/convert_fp16.py; same predictions as the
# float32 originals on every test set), then the code (changes often).
COPY models/intent_model_v2_fp16 models/intent_model_v2_fp16
COPY models/entity_model_fp16 models/entity_model_fp16
COPY .streamlit/config.toml .streamlit/config.toml
COPY app app
COPY app_pages app_pages
COPY main.py Dashboard.py chat_cli.py ./
COPY tests/send_test_webhook.py tests/send_test_webhook.py

USER aura
EXPOSE 8000 8501
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
