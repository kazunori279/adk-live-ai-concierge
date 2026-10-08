"""Google Home MCP client and read-only state query tool for live voice agent.

Wraps Google Home MCP server (home.googleapis.com) as a sub-agent tool.
Exposes read-only state querying (list_homes, list_home_resources,
list_home_states, list_home_history) while preventing routing drift on
the Live Agent.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import pathlib
import time
from typing import Any

import httpx
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool, ToolContext
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from .. import screen
from ._lang import of_turn

DEFAULT_HOME_MCP_URL = "https://home.googleapis.com/mcp"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
UNCONFIGURED = "Google Home is not configured on this terminal."
REFRESH_MARGIN_SECONDS = 300  # Refresh 5 minutes before expiry

DESCRIPTION = (
    "Google Home: query smart home devices and control devices in the living "
    "room (リビング). Reads cover all rooms; device control is restricted "
    "strictly to the living room."
)

HOME_INSTRUCTION = """\
You are the Home sub-agent for Google Home.
Your job is to query smart home device states and execute allowed living room device actions:
- list_homes: lists accessible homes and structures.
- list_home_resources: lists devices, rooms, traits, and metadata.
- list_home_states: queries live state of devices across all rooms (unrestricted read).
- list_home_history: queries state change history over a time range.
- run_home_actions: controls devices strictly in the living room (リビング).

Control scope rules:
- Control is ONLY permitted for devices in the "リビング" (living room).
- Requests to control devices in any other room (寝室, 寝室3, 廊下, 玄関, 浴室, その他, etc.) must be refused with the explanation: "リビング以外の機器は操作できません".
- Living room appliances (air conditioner, fan) are controlled via their smart plugs ("エアコン", "ファン"). When asked to turn them on or off, target the living room plug device. Thermostats and appliance devices in "その他" are not controllable; temperature, mode, and fan speed changes cannot be controlled.
- Structure IDs are handled automatically.
- Provide a concise, direct answer in the requested language.
"""


class HomeError(Exception):
    """Base error for Google Home MCP operations."""


class HomeAuthError(HomeError):
    """Raised when authentication credentials are missing or token refresh fails."""


try:
    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
except (ImportError, ModuleNotFoundError):
    class StreamableHTTPConnectionParams(BaseModel):
        """Parameters for MCP Streamable HTTP connection."""
        model_config = ConfigDict(arbitrary_types_allowed=True)
        url: str = DEFAULT_HOME_MCP_URL
        headers: dict[str, Any] | None = None
        timeout: float = 10.0
        sse_read_timeout: float = 60 * 5.0
        terminate_on_close: bool = True


def config_dir() -> pathlib.Path:
    """Directory holding Home OAuth client and token files."""
    env_dir = os.getenv("HOME_CONFIG_DIR") or os.getenv("SDM_CONFIG_DIR")
    if env_dir:
        return pathlib.Path(env_dir)
    return pathlib.Path.home() / ".config" / "ai-terminal"


def client_path() -> pathlib.Path:
    return config_dir() / "home-mcp-client.json"


def token_path() -> pathlib.Path:
    return config_dir() / "home-mcp-token.json"


# In-memory access token and structure ID cache
_token_cache: dict[str, Any] = {
    "access_token": None,
    "expires_at": 0.0,
}
_token_lock = asyncio.Lock()
_cached_structure_id: str | None = None
_client: httpx.AsyncClient | None = None
_agent: Agent | None = None
_req_id_counter = 0
_HOME_COLLECTOR: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("_HOME_COLLECTOR", default=None)


def get_cached_structure_id() -> str | None:
    return _cached_structure_id


def set_cached_structure_id(structure_id: str | None) -> None:
    global _cached_structure_id
    _cached_structure_id = structure_id


async def get_access_token(force_refresh: bool = False) -> str:
    """Obtain a valid OAuth2 access token with in-memory TTL caching."""
    c_path = client_path()
    t_path = token_path()

    if not c_path.is_file() or not t_path.is_file():
        raise HomeAuthError(
            f"Google Home credentials not found in {config_dir()}. "
            "home-mcp-client.json and home-mcp-token.json are required."
        )

    now = time.time()
    cached_token = _token_cache.get("access_token")
    expires_at = _token_cache.get("expires_at", 0.0)

    # In-memory access token cache with TTL (refresh 5 minutes before expiry)
    if not force_refresh and cached_token and now < (expires_at - REFRESH_MARGIN_SECONDS):
        return cached_token

    async with _token_lock:
        now = time.time()
        cached_token = _token_cache.get("access_token")
        expires_at = _token_cache.get("expires_at", 0.0)
        if not force_refresh and cached_token and now < (expires_at - REFRESH_MARGIN_SECONDS):
            return cached_token

        try:
            with open(c_path, "r", encoding="utf-8") as f:
                client_raw = json.load(f)
            client_info = (
                client_raw.get("installed")
                or client_raw.get("web")
                or client_raw
            )
            client_id = client_info["client_id"]
            client_secret = client_info["client_secret"]

            with open(t_path, "r", encoding="utf-8") as f:
                token_info = json.load(f)
            refresh_token = token_info.get("refresh_token")
            if not refresh_token:
                raise HomeAuthError("No refresh_token found in home-mcp-token.json")
        except Exception as exc:
            if isinstance(exc, HomeAuthError):
                raise
            raise HomeAuthError(f"Failed to read Home credentials: {exc}") from exc

        req_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(OAUTH_TOKEN_URL, data=req_data)
                if resp.status_code != 200:
                    raise HomeAuthError(f"OAuth token refresh HTTP error {resp.status_code}: {resp.text}")
                data = resp.json()
                new_token = data.get("access_token")
                if not new_token:
                    raise HomeAuthError("No access_token returned in refresh response")
                expires_in = float(data.get("expires_in", 3600))
                _token_cache["access_token"] = new_token
                _token_cache["expires_at"] = time.time() + expires_in
                return new_token
        except httpx.HTTPError as exc:
            raise HomeAuthError(f"Token refresh request failed: {exc}") from exc
        except HomeAuthError:
            raise
        except Exception as exc:
            raise HomeAuthError(f"Unexpected error refreshing token: {exc}") from exc


def extract_structure_id(payload: Any) -> str | None:
    """Extract structureId from list_homes result or payload."""
    if not payload:
        return None
    if isinstance(payload, dict):
        for key in ("structures", "homes"):
            items = payload.get(key)
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    sid = first.get("id") or first.get("structureId") or first.get("name")
                    if sid:
                        return sid
        sid = payload.get("structureId") or payload.get("structure_id") or payload.get("id")
        if sid:
            return sid
    elif isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first.get("id") or first.get("structureId") or first.get("name")
    return None


def extract_structured_content(mcp_response: dict[str, Any]) -> Any:
    """Extract structured payload from Google Home MCP result.

    Google Home MCP returns real payloads in `structured_content` while
    `content[].text` contains empty JSON `{}`.
    """
    if not isinstance(mcp_response, dict):
        return mcp_response

    result = mcp_response.get("result", mcp_response)
    if not isinstance(result, dict):
        return result

    # 1. Top-level structured_content inside result
    if "structured_content" in result and result["structured_content"]:
        return result["structured_content"]

    # 2. Inside content items
    content = result.get("content", [])
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if "structured_content" in item and item["structured_content"]:
                    return item["structured_content"]
                text = item.get("text", "")
                if text and text.strip() not in ("{}", ""):
                    try:
                        return json.loads(text)
                    except Exception:
                        return text

    return result


def _get_http_client(timeout: float = 15.0) -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


async def call_mcp_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    server_url: str = DEFAULT_HOME_MCP_URL,
    token: str | None = None,
) -> dict[str, Any]:
    """Execute a tool call against the Google Home MCP server."""
    global _req_id_counter, _cached_structure_id
    args = dict(arguments) if arguments else {}

    # Structure resolution: inject structureId into resource/state/history/action calls
    if name in ("list_home_resources", "list_home_states", "list_home_history", "run_home_actions"):
        if not args.get("structureId") and not args.get("structure_id"):
            if _cached_structure_id is None:
                homes_res = await call_mcp_tool("list_homes", {}, server_url=server_url, token=token)
                homes_payload = extract_structured_content(homes_res)
                sid = extract_structure_id(homes_payload)
                if sid:
                    _cached_structure_id = sid
            if _cached_structure_id:
                args["structureId"] = _cached_structure_id

    auth_token = token or await get_access_token()
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    _req_id_counter += 1
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": _req_id_counter,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": args,
        },
    }

    client = _get_http_client()
    resp = await client.post(server_url, headers=headers, json=rpc_payload)
    if resp.status_code in (401, 403):
        new_token = await get_access_token(force_refresh=True)
        headers["Authorization"] = f"Bearer {new_token}"
        resp = await client.post(server_url, headers=headers, json=rpc_payload)

    if resp.status_code != 200:
        raise HomeError(f"Google Home MCP HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    if "error" in data:
        raise HomeError(f"Google Home MCP error: {data['error']}")

    if name == "list_homes":
        payload = extract_structured_content(data)
        sid = extract_structure_id(payload)
        if sid:
            _cached_structure_id = sid

    return data


# Read-only state querying tools
async def list_homes() -> dict[str, Any]:
    """List accessible homes and structures."""
    raw = await call_mcp_tool("list_homes", {})
    return extract_structured_content(raw)


async def list_home_resources(structureId: str | None = None) -> dict[str, Any]:
    """List devices, rooms, traits, and metadata for a structure."""
    args = {"structureId": structureId} if structureId else {}
    raw = await call_mcp_tool("list_home_resources", args)
    payload = extract_structured_content(raw)
    if (collector := _HOME_COLLECTOR.get()) is not None:
        collector["resources"] = payload
    return payload


async def list_home_states(structureId: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Query live states of devices across all rooms (unrestricted)."""
    args = {"structureId": structureId, **kwargs} if structureId else {**kwargs}
    raw = await call_mcp_tool("list_home_states", args)
    payload = extract_structured_content(raw)
    if (collector := _HOME_COLLECTOR.get()) is not None:
        collector["states"] = payload
    return payload


async def list_home_history(structureId: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Query state history and event logs for devices."""
    args = {"structureId": structureId, **kwargs} if structureId else {**kwargs}
    raw = await call_mcp_tool("list_home_history", args)
    payload = extract_structured_content(raw)
    if (collector := _HOME_COLLECTOR.get()) is not None:
        collector["history"] = payload
    return payload


LIVING_ROOM_NAME = "リビング"
REFUSAL_MESSAGE = "リビング以外の機器は操作できません"


def extract_device_room_map(resources: dict[str, Any]) -> dict[str, str]:
    """Extract a mapping of device identifiers and display names to room names.

    Keys dynamically on the room metadata returned by list_home_resources,
    supporting devices/rooms lists and generic resource collections.
    """
    if not isinstance(resources, dict):
        return {}

    room_map: dict[str, str] = {}
    room_names_by_id: dict[str, str] = {}

    rooms = resources.get("rooms", [])
    raw_resources = resources.get("resources", [])

    for r in rooms:
        if isinstance(r, dict):
            r_id = r.get("id") or r.get("name")
            r_name = r.get("displayName") or r.get("name") or ""
            if r_id and r_name:
                room_names_by_id[str(r_id)] = str(r_name)
            for dev in r.get("devices", []):
                if isinstance(dev, str):
                    room_map[dev] = str(r_name)
                elif isinstance(dev, dict):
                    dev_id = dev.get("id") or dev.get("name")
                    dev_name = dev.get("displayName") or dev.get("name")
                    if dev_id:
                        room_map[str(dev_id)] = str(r_name)
                    if dev_name:
                        room_map[str(dev_name)] = str(r_name)

    for item in raw_resources:
        if isinstance(item, dict) and item.get("type") in ("Room", "room"):
            r_id = item.get("id") or item.get("name")
            r_name = item.get("displayName") or item.get("name") or ""
            if r_id and r_name:
                room_names_by_id[str(r_id)] = str(r_name)
            for dev in item.get("devices", []):
                if isinstance(dev, str):
                    room_map[dev] = str(r_name)
                elif isinstance(dev, dict):
                    dev_id = dev.get("id") or dev.get("name")
                    dev_name = dev.get("displayName") or dev.get("name")
                    if dev_id:
                        room_map[str(dev_id)] = str(r_name)
                    if dev_name:
                        room_map[str(dev_name)] = str(r_name)

    devices = resources.get("devices", [])
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        dev_id = dev.get("id") or dev.get("name")
        dev_name = dev.get("displayName") or dev.get("name")
        raw_room = (
            dev.get("room")
            or dev.get("roomName")
            or dev.get("room_name")
            or dev.get("space")
            or dev.get("roomId")
        )
        resolved_room = ""
        if raw_room:
            raw_str = str(raw_room)
            resolved_room = room_names_by_id.get(raw_str, raw_str)
        elif dev.get("parent"):
            parent_str = str(dev.get("parent"))
            resolved_room = room_names_by_id.get(parent_str, parent_str)

        parent_ids = dev.get("parentIds") or []
        if isinstance(parent_ids, str):
            parent_ids = [parent_ids]
        if not resolved_room and isinstance(parent_ids, list):
            for pid in parent_ids:
                if str(pid) in room_names_by_id:
                    resolved_room = room_names_by_id[str(pid)]
                    break

        if resolved_room:
            if dev_id:
                room_map[str(dev_id)] = resolved_room
            if dev_name:
                room_map[str(dev_name)] = resolved_room

    for item in raw_resources:
        if not isinstance(item, dict) or item.get("type") in ("Room", "room"):
            continue
        dev_id = item.get("id") or item.get("name")
        dev_name = item.get("displayName") or item.get("name")
        raw_room = (
            item.get("room")
            or item.get("roomName")
            or item.get("room_name")
            or item.get("space")
            or item.get("roomId")
        )
        resolved_room = ""
        if raw_room:
            raw_str = str(raw_room)
            resolved_room = room_names_by_id.get(raw_str, raw_str)
        elif item.get("parent"):
            parent_str = str(item.get("parent"))
            resolved_room = room_names_by_id.get(parent_str, parent_str)

        parent_ids = item.get("parentIds") or []
        if isinstance(parent_ids, str):
            parent_ids = [parent_ids]
        if not resolved_room and isinstance(parent_ids, list):
            for pid in parent_ids:
                if str(pid) in room_names_by_id:
                    resolved_room = room_names_by_id[str(pid)]
                    break

        if resolved_room:
            if dev_id:
                room_map[str(dev_id)] = resolved_room
            if dev_name:
                room_map[str(dev_name)] = resolved_room

    return room_map


def build_home_summary_payload(states: dict[str, Any], resources: dict[str, Any] | None = None) -> dict[str, Any]:
    """Format room-by-room device states for Home Summary Card."""
    dev_to_room: dict[str, str] = {}
    if resources and isinstance(resources, dict):
        dev_to_room = extract_device_room_map(resources)

    rooms_map: dict[str, list[dict[str, Any]]] = {}
    devices = states.get("devices", []) if isinstance(states, dict) else []
    for dev in devices:
        d_id = dev.get("id") or dev.get("name")
        d_name = dev.get("displayName") or dev.get("name") or "Device"
        room = (
            dev.get("room")
            or (dev_to_room.get(str(d_id)) if d_id else None)
            or (dev_to_room.get(str(d_name)) if d_name else None)
            or "その他"
        )
        if room not in rooms_map:
            rooms_map[room] = []
        state_val = dev.get("state") or ("on" if dev.get("on") else "off")
        rooms_map[room].append({
            "name": d_name,
            "state": state_val,
            "detail": dev.get("detail", ""),
        })
    rooms_list = [{"name": r, "devices": devs} for r, devs in rooms_map.items()]
    return {"type": "home", "card_type": "summary", "rooms": rooms_list}


def build_home_timeline_payload(history: dict[str, Any]) -> dict[str, Any]:
    """Format event history entries for Home Timeline Card."""
    events = history.get("events", []) if isinstance(history, dict) else []
    event_list = []
    for ev in events:
        event_list.append({
            "time": ev.get("time") or ev.get("timestamp") or "",
            "device": ev.get("device") or ev.get("deviceName") or "",
            "event": ev.get("event") or ev.get("description") or "",
        })
    return {"type": "home", "card_type": "timeline", "events": event_list}


async def run_home_actions(
    homeActionRequests: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    structureId: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute device control actions strictly scoped to living room devices.

    Server-side scope guard: queries resources to inspect room assignment.
    If any target device is outside 'リビング', the request is refused server-side
    before the MCP call is dispatched.
    """
    raw_requests = homeActionRequests or kwargs.get("homeActionRequests") or actions or kwargs.get("actions") or []
    if not raw_requests and ("deviceId" in kwargs or "device_id" in kwargs or "id" in kwargs):
        raw_requests = [
            {
                "id": kwargs.get("id") or kwargs.get("deviceId") or kwargs.get("device_id"),
                "type": kwargs.get("type", ""),
                "command": kwargs.get("command") or kwargs.get("commands", []),
            }
        ]

    if not raw_requests or not isinstance(raw_requests, list):
        return {
            "status": "refused",
            "refused": True,
            "error": REFUSAL_MESSAGE,
            "answer": REFUSAL_MESSAGE,
        }

    target_devices: list[str] = []
    formatted_requests: list[dict[str, Any]] = []

    for req in raw_requests:
        if not isinstance(req, dict):
            return {
                "status": "refused",
                "refused": True,
                "error": REFUSAL_MESSAGE,
                "answer": REFUSAL_MESSAGE,
            }
        dev_id = req.get("id") or req.get("deviceId") or req.get("device_id")
        if not dev_id:
            return {
                "status": "refused",
                "refused": True,
                "error": REFUSAL_MESSAGE,
                "answer": REFUSAL_MESSAGE,
            }
        dev_id_str = str(dev_id)
        target_devices.append(dev_id_str)
        formatted_req = dict(req)
        formatted_req["id"] = dev_id_str
        formatted_requests.append(formatted_req)

    resources = await list_home_resources(structureId=structureId)
    room_map = extract_device_room_map(resources)

    # Server-side guard: verify EVERY target device is dynamically resolved to 'リビング'
    for dev_id in target_devices:
        room = room_map.get(dev_id)
        if room != LIVING_ROOM_NAME:
            return {
                "status": "refused",
                "refused": True,
                "error": REFUSAL_MESSAGE,
                "answer": REFUSAL_MESSAGE,
            }

    args: dict[str, Any] = {"homeActionRequests": formatted_requests}
    if structureId:
        args["structureId"] = structureId

    raw = await call_mcp_tool("run_home_actions", args)
    return extract_structured_content(raw)


class HomeRequest(BaseModel):
    """Schema for home queries."""
    request: str = Field(..., description="Query or directive about Google Home devices or states")
    context: str = Field("", description="Context from earlier turns in the conversation")
    lang: str = Field("", description="BCP-47 language tag of the user (e.g. ja, en)")


def build_home_agent() -> Agent:
    """Build the Google Home sub-agent with read and control tools."""
    return Agent(
        name="home",
        description=DESCRIPTION,
        input_schema=HomeRequest,
        model=Gemini(
            model=os.getenv("HOME_MODEL", "gemini-3.8-flash"),
            client_kwargs={"location": os.getenv("HOME_LOCATION", "global")},
        ),
        instruction=HOME_INSTRUCTION,
        tools=[
            FunctionTool(list_homes),
            FunctionTool(list_home_resources),
            FunctionTool(list_home_states),
            FunctionTool(list_home_history),
            FunctionTool(run_home_actions),
        ],
    )


async def _run_home_subagent(
    request: str,
    context: str = "",
    lang: str = "",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    global _agent
    if _agent is None:
        _agent = build_home_agent()

    try:
        app_name = getattr(_agent, "name", "home")
        sessions = InMemorySessionService()
        session = await sessions.create_session(app_name=app_name, user_id="terminal-user")
        runner = Runner(app_name=app_name, agent=_agent, session_service=sessions)

        req_payload = {"request": request, "context": context, "lang": lang}
        msg = types.Content(
            role="user",
            parts=[types.Part.from_text(text=json.dumps(req_payload, ensure_ascii=False))],
        )

        last_content = None
        error_msg = None
        try:
            async with Aclosing(runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=msg,
            )) as events:
                async for event in events:
                    if getattr(event, "error_message", None):
                        error_msg = event.error_message
                    if getattr(event, "content", None):
                        last_content = event.content
        finally:
            await runner.close()

        if last_content and last_content.parts:
            texts = [p.text for p in last_content.parts if p.text and not getattr(p, "thought", False)]
            if texts:
                return {"answer": "\n".join(texts)}

        if error_msg:
            return {"answer": f"Google Home query failed: {error_msg}"}

        return {"answer": "No response from Google Home."}
    except Exception as exc:
        return {"answer": f"Google Home error: {exc}"}


async def home(
    request: str,
    tool_context: ToolContext = None,
    context: str = "",
    lang: str = "",
) -> dict[str, Any]:
    """Query Google Home smart home devices, states, rooms, and history.

    Args:
        request: Direct question or query about smart home devices/states.
        tool_context: Live tool context.
        context: Context from earlier turns in the conversation.
        lang: User language (e.g. ja, en).
    """
    lang = of_turn(lang, tool_context)
    try:
        await get_access_token()
    except HomeAuthError:
        return {"answer": UNCONFIGURED}
    except Exception:
        return {"answer": UNCONFIGURED}

    collector: dict[str, Any] = {}
    token = _HOME_COLLECTOR.set(collector)
    try:
        res = await _run_home_subagent(request, context=context, lang=lang, tool_context=tool_context)
    finally:
        _HOME_COLLECTOR.reset(token)

    session_id = ""
    if tool_context:
        try:
            session_id = screen.session_of(tool_context)
        except Exception:
            session_id = getattr(tool_context, "session_id", "")
    if session_id:
        if collector.get("states"):
            payload = build_home_summary_payload(collector["states"], collector.get("resources"))
            await screen.push(session_id, payload)
        elif collector.get("history"):
            payload = build_home_timeline_payload(collector["history"])
            await screen.push(session_id, payload)

    return res


def build_home_tool() -> FunctionTool:
    """Wrap the Google Home sub-agent as a tool the live agent can call."""
    global _agent
    _agent = build_home_agent()
    tool = FunctionTool(home)
    tool.response_scheduling = types.FunctionResponseScheduling.INTERRUPT
    return tool


class _CloseResult:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    def __await__(self):
        return self._run().__await__()

    async def _run(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


def close_home_toolset() -> _CloseResult:
    """Close MCP HTTP client session and clear cached structure and tokens."""
    global _client, _cached_structure_id, _token_cache, _agent
    _cached_structure_id = None
    _token_cache = {"access_token": None, "expires_at": 0.0}
    _agent = None
    old_client = _client
    _client = None
    if old_client is not None:
        try:
            loop = asyncio.get_running_loop()
            if not old_client.is_closed:
                loop.create_task(old_client.aclose())
        except RuntimeError:
            if not old_client.is_closed:
                asyncio.run(old_client.aclose())
    return _CloseResult(old_client)
