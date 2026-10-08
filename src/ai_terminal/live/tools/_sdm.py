"""Smart Device Management (SDM) API WebRTC stream helper.

Manages WebRTC live video streams for Nest cameras (Nest Hub Max, Nest Cam).
Authentication is handled via OAuth2 credentials stored in ~/.config/ai-terminal/
(home-mcp-client.json and sdm-token.json).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_ID = ""
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
SDM_API_BASE = "https://smartdevicemanagement.googleapis.com/v1"

_CACHED_DEVICE_NAME: str | None = None


class SdmError(Exception):
    """Base error for Smart Device Management API operations."""


class SdmAuthError(SdmError):
    """Raised when SDM authentication fails or token is expired/invalid."""


def config_dir() -> pathlib.Path:
    """Directory holding SDM OAuth client and token files."""
    env_dir = os.getenv("SDM_CONFIG_DIR")
    if env_dir:
        return pathlib.Path(env_dir)
    return pathlib.Path.home() / ".config" / "ai-terminal"


def project_id() -> str:
    """Device Access project ID."""
    value = os.getenv("SDM_PROJECT_ID", DEFAULT_PROJECT_ID).strip()
    if not value:
        raise SdmAuthError("Set SDM_PROJECT_ID to your Device Access project ID.")
    return value


def get_access_token() -> str:
    """Load client credentials and refresh token to obtain a fresh access token."""
    cdir = config_dir()
    client_path = cdir / "home-mcp-client.json"
    token_path = cdir / "sdm-token.json"

    if not client_path.is_file() or not token_path.is_file():
        raise SdmAuthError(
            f"SDM credentials not found in {cdir}. "
            "home-mcp-client.json and sdm-token.json are required."
        )

    try:
        with open(client_path, "r", encoding="utf-8") as f:
            client_raw = json.load(f)
        client_info = (
            client_raw.get("web")
            or client_raw.get("installed")
            or client_raw
        )
        client_id = client_info["client_id"]
        client_secret = client_info["client_secret"]

        with open(token_path, "r", encoding="utf-8") as f:
            token_info = json.load(f)
        refresh_token = token_info.get("refresh_token")
        if not refresh_token:
            raise SdmAuthError("No refresh_token found in sdm-token.json")
    except Exception as exc:
        if isinstance(exc, SdmAuthError):
            raise
        raise SdmAuthError(f"Failed to read SDM credentials: {exc}") from exc

    req_data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode("utf-8")

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=req_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["access_token"]
    except urllib.error.HTTPError as exc:
        raise SdmAuthError(f"Token refresh HTTP error: {exc.code}") from exc
    except Exception as exc:
        raise SdmAuthError(f"Token refresh failed: {exc}") from exc


def sdm_call(
    path: str,
    payload: dict[str, Any] | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Execute an authenticated call to the Smart Device Management API."""
    token = access_token or get_access_token()
    url = f"{SDM_API_BASE}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SdmAuthError("カメラの再認証が必要です") from exc
        raw_err = exc.read().decode("utf-8", errors="replace")
        raise SdmError(f"SDM API error {exc.code}: {raw_err}") from exc
    except SdmAuthError:
        raise
    except Exception as exc:
        raise SdmError(f"SDM request failed: {exc}") from exc


def get_camera_device() -> str:
    """Find the primary camera device resource name."""
    global _CACHED_DEVICE_NAME
    if _CACHED_DEVICE_NAME:
        return _CACHED_DEVICE_NAME

    env_device = os.getenv("SDM_DEVICE_NAME")
    if env_device:
        _CACHED_DEVICE_NAME = env_device
        return env_device

    proj = project_id()
    data = sdm_call(f"enterprises/{proj}/devices")
    devices = data.get("devices", [])
    for dev in devices:
        traits = dev.get("traits", {})
        if "sdm.devices.traits.CameraLiveStream" in traits:
            _CACHED_DEVICE_NAME = dev["name"]
            return dev["name"]

    if devices:
        _CACHED_DEVICE_NAME = devices[0]["name"]
        return _CACHED_DEVICE_NAME

    raise SdmError("No camera device found with CameraLiveStream trait")


def generate_webrtc_stream(offer_sdp: str) -> dict[str, Any]:
    """Call GenerateWebRtcStream on camera device."""
    device_name = get_camera_device()
    endpoint = f"{device_name}:executeCommand"
    payload = {
        "command": "sdm.devices.commands.CameraLiveStream.GenerateWebRtcStream",
        "params": {"offerSdp": offer_sdp},
    }
    resp = sdm_call(endpoint, payload=payload)
    results = resp.get("results", {})
    answer_sdp = results.get("answerSdp", "")
    media_session_id = results.get("mediaSessionId", "")
    expires_at = results.get("expiresAt", "")
    logger.info(
        "SDM Camera: GenerateWebRtcStream succeeded for %s", media_session_id
    )
    return {
        "answerSdp": answer_sdp,
        "mediaSessionId": media_session_id,
        "expiresAt": expires_at,
    }


def extend_webrtc_stream(media_session_id: str) -> dict[str, Any]:
    """Extend an active WebRTC stream session."""
    device_name = get_camera_device()
    endpoint = f"{device_name}:executeCommand"
    payload = {
        "command": "sdm.devices.commands.CameraLiveStream.ExtendWebRtcStream",
        "params": {"mediaSessionId": media_session_id},
    }
    resp = sdm_call(endpoint, payload=payload)
    results = resp.get("results", {})
    media_session_id = results.get("mediaSessionId", media_session_id)
    expires_at = results.get("expiresAt", "")
    logger.info(
        "SDM Camera: ExtendWebRtcStream for %s, expiresAt: %s",
        media_session_id,
        expires_at,
    )
    return {
        "mediaSessionId": media_session_id,
        "expiresAt": expires_at,
    }


def stop_webrtc_stream(media_session_id: str) -> dict[str, Any]:
    """Stop an active WebRTC stream session and turn off camera LED."""
    device_name = get_camera_device()
    endpoint = f"{device_name}:executeCommand"
    payload = {
        "command": "sdm.devices.commands.CameraLiveStream.StopWebRtcStream",
        "params": {"mediaSessionId": media_session_id},
    }
    resp = sdm_call(endpoint, payload=payload)
    logger.info("SDM Camera: StopWebRtcStream for %s", media_session_id)
    return resp.get("results", {})
