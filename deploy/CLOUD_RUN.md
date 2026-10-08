# Cloud Run Demo Server Deployment

Deploy the AI Terminal live server (`live-server`) to Google Cloud Run as a web demo accessible from any browser without physical Raspberry Pi hardware.

## Architecture & Container Configuration

Cloud Run runs the containerized FastAPI server with:
- **WebSocket Streaming**: Cloud Run natively supports WebSockets. Deployment sets `--timeout=3600` and `--session-affinity` so streaming voice sessions survive connection pauses and reconnect to the same instance.
- **Dynamic Port**: The container listens on dynamic `$PORT` (default 8080) bound to `0.0.0.0`.
- **Ephemeral Storage**: Cloud Run filesystems are read-only except `/tmp`. State paths are redirected to `/tmp`:
  - `SESSIONS_DB=/tmp/sessions.db`
  - `GWS_CACHE_DIR=/tmp/gws_cache`
  - `SCREENSAVER_DIR=/tmp/screensaver`
- **Host & Origin Validation**: `ALLOWED_HOSTS="ai-terminal-demo-example-ue.a.run.app,ai-terminal-demo-000000000000.us-east1.run.app"` restricts allowed requests to the service's specific hostnames through Starlette's `TrustedHostMiddleware`. It is a host allowlist matched against HTTP `Host` headers, so bare hostnames must be used; specifying a URL with `https://` causes `TrustedHostMiddleware` to refuse all requests with an `Invalid host header` error. The client automatically uses `wss://` over HTTPS.
- **Management Routes Gating**: Sensitive management endpoints (`/api/gws/logout`, `/api/forget/{user_id}`, `/api/wake`, and `/api/reboot`) are disabled by default in demo environments because `ENABLE_MANAGEMENT_ROUTES` is not set, preventing unauthenticated visitors from terminating or resetting the demo.
- **External Tool Binaries (`gws` & Chromium)**: The container image packages external binaries needed by demo tools:
  - `gws`: Official standalone Google Workspace CLI binary installed to `/usr/local/bin/gws`.
  - Playwright Chromium: Browser binaries and system libraries installed under `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`.

## Prerequisites

1. **Google Cloud SDK**:
   Ensure `gcloud` CLI is installed and authenticated:
   ```bash
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   ```

2. **Required GCP APIs**:
   Enable Cloud Run and Cloud Build:
   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com
   ```

3. **Required API Keys & Secrets**:
   - `GOOGLE_GENAI_API_KEY`: API key for Gemini Live audio streaming (or configure Vertex AI service account credentials).
   - `GOOGLE_MAPS_API_KEY`: Browser Maps JS API key. Add `https://*.run.app/*` to HTTP Referrer restrictions in Google Cloud Console.
   - `GOOGLE_APPLICATION_CREDENTIALS`: Path to service account credentials for Google Workspace operations (throwaway account `user@example.com`).
   - Optional: `GOOGLE_SEARCH_API_KEY` and `GOOGLE_SEARCH_ENGINE_ID` for Search grounding.

## Deployment Steps

### 1. Verify Prerequisites
Run the deployment checker to confirm the Dockerfile and configuration files are present:
```bash
uv run python tools/cloud_run_deploy.py check
```

### 2. Dry Run
Inspect the command before submitting to Cloud Run:
```bash
uv run python tools/cloud_run_deploy.py deploy --dry-run
```

### 3. Deploy
Deploy using the helper tool:
```bash
uv run python tools/cloud_run_deploy.py deploy \
  --project YOUR_PROJECT_ID \
  --region us-central1 \
  --service ai-terminal-demo
```

Alternatively, invoke `gcloud` directly:
```bash
gcloud run deploy ai-terminal-demo \
  --source . \
  --region us-central1 \
  --timeout=3600 \
  --session-affinity \
  --allow-unauthenticated \
  --set-env-vars SESSIONS_DB=/tmp/sessions.db,GWS_CACHE_DIR=/tmp/gws_cache,SCREENSAVER_DIR=/tmp/screensaver,ALLOWED_HOSTS="ai-terminal-demo-example-ue.a.run.app,ai-terminal-demo-000000000000.us-east1.run.app"
```

To provide secret keys securely, use Cloud Secret Manager or pass them via `--set-secrets`:
```bash
gcloud run services update ai-terminal-demo \
  --region us-central1 \
  --set-secrets GOOGLE_GENAI_API_KEY=google-genai-api-key:latest,GOOGLE_MAPS_API_KEY=google-maps-api-key:latest
```

## Testing the Deployed Service

1. Open the Cloud Run Service URL (`https://ai-terminal-demo-XXXX-uc.a.run.app`) in Chrome or Safari.
2. Grant microphone permission when prompted.
3. Tap the microphone icon to begin voice interaction, or type questions into the input field.
