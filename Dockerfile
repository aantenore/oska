# Build the wheel in one stage, install it in a slim one. Nothing from the
# build stage (compilers, caches, the source tree) reaches the runtime image.
FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /wheels

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --home-dir /srv/oska --shell /usr/sbin/nologin oska \
    && mkdir -p /srv/oska/state \
    && chown -R oska:oska /srv/oska

COPY --from=builder /wheels /wheels
RUN pip install "$(ls /wheels/oska-*.whl)[api,postgres]" && rm -rf /wheels

WORKDIR /srv/oska
# The example operations catalog and its connector bindings. They are read at
# start-up, relative to the working directory.
COPY --chown=oska:oska examples/ops ./examples/ops

# Paths and state are configuration (docs/configuration.md). Override them with
# environment variables or by mounting a runtime.json and pointing
# OSKA_RUNTIME_CONFIG at it.
ENV OSKA_SKILLS_ROOTS=examples/ops/skills \
    OSKA_CONNECTORS_CONFIG=examples/ops/configs/connectors.json \
    OSKA_STATE_DIR=/srv/oska/state

USER oska
VOLUME ["/srv/oska/state"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# Listens on every interface of the container, so `serve` warns that identity is
# not required. That is the demo image: see docs/running-in-containers.md.
CMD ["oska", "serve", "--host", "0.0.0.0", "--port", "8000"]
