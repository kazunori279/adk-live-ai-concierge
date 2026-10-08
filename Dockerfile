FROM python:3.13-slim

# Install system dependencies needed for PortAudio and runtime tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

# Install Google Workspace CLI (gws) from official release
ARG GWS_VERSION=v0.22.5
RUN curl -sLO "https://github.com/googleworkspace/cli/releases/download/${GWS_VERSION}/google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz" \
    && tar -xzf "google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz" -C /usr/local/bin ./gws \
    && chmod +x /usr/local/bin/gws \
    && rm -f "google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz"

# Copy uv binary from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install project dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Install Playwright Chromium and OS library dependencies
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv run playwright install --with-deps chromium

# Copy source code and install project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Runtime environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV HOST=0.0.0.0
ENV PORT=8080
ENV ALLOWED_HOSTS="ai-terminal-demo-example-ue.a.run.app,ai-terminal-demo-000000000000.us-east1.run.app"
ENV SESSIONS_DB=/tmp/sessions.db
ENV GWS_CACHE_DIR=/tmp/gws_cache
ENV SCREENSAVER_DIR=/tmp/screensaver

EXPOSE 8080

CMD ["sh", "-c", "exec live-server --host 0.0.0.0 --port ${PORT}"]
