# =============================================================================
# DataShield backend.
#
# Multi-stage. The previous single-stage image shipped `build-essential` (its
# comment claimed the build deps were removed, but only the apt *lists* were)
# and installed the `[dev]` extra, so pytest and ruff went to production too.
# A compiler and a test runner in an internet-facing image are attack surface
# for no benefit, and a security reviewer will ask about both.
#
# Targets:
#   runtime  (default) — what deploys. No compiler, no dev tooling, non-root.
#   dev                — runtime plus pytest/ruff, used by `cms-test` in compose.
# =============================================================================

# --- builder: compile wheels, then throw the toolchain away -------------------
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# argon2-cffi needs a compiler. It needs one HERE, and nowhere else.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# A venv is the unit we copy forward — simpler to relocate than picking
# site-packages apart, and it keeps the runtime stage free of pip's bookkeeping.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .


# --- runtime -------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# The venv only. Nothing else is carried over from the builder.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY . .

# Non-root. A container that runs as root turns a code-execution bug into a host
# problem.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /srv
USER appuser

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=2).status == 200 else 1)"

# No `alembic upgrade head` here, on purpose. Migrations are a separate step —
# the `cms-migrate` service in compose, a Container Apps job in Azure. With more
# than one replica every replica would otherwise race to migrate on boot, and
# the app runs on a restricted role that deliberately cannot alter the schema.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]


# --- dev: the same runtime, plus test tooling -----------------------------------
FROM runtime AS dev

USER root
RUN pip install --no-cache-dir \
      pytest==8.3.4 pytest-asyncio==0.25.0 ruff==0.8.4
USER appuser
