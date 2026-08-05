# eSIM MCP server -- Streamable HTTP deployment image.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependency layer: only the metadata, so the layer caches across code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 esim
USER esim

# Configuration is supplied at runtime (ESIM_API_BASE_URL, ESIM_MCP_DEVICE_ID_SALT, ...).
# No secrets are baked into the image.
ENV ESIM_MCP_TRANSPORT=streamable-http \
    ESIM_MCP_HOST=0.0.0.0 \
    ESIM_MCP_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "esim_mcp.server"]
