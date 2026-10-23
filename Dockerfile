FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

# Install from uv.lock so an image rebuild reproduces the tested dependency
# set instead of resolving the newest releases allowed by pyproject.toml.
# The oauth extra is installed so OAUTH_* deployments do not depend on mcp
# happening to pull in pyjwt.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --extra oauth

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

CMD ["python", "-m", "obsidian_livesync_mcp.server"]
