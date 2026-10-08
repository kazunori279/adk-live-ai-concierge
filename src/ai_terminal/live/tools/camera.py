"""Camera tool for showing live camera video card on the terminal screen."""

from __future__ import annotations

from google.adk.tools import FunctionTool, ToolContext

from .. import screen

DESCRIPTION = (
    "Show live camera stream from a home camera (e.g. Nest Hub in living room)."
)


async def camera(device: str = "", tool_context: ToolContext = None) -> dict:
    """Show the live camera stream from a home camera on the terminal display.

    Args:
        device: Target camera name (e.g. "living_room"). Defaults to "living_room".
    """
    device_name = device or "living_room"
    title = "リビングのホーム"
    turn_id = ""
    session_id = ""
    if tool_context:
        session_id = getattr(getattr(tool_context, "session", None), "id", "") or getattr(tool_context, "session_id", "")
        turn_id = getattr(tool_context, "function_call_id", "") or getattr(tool_context, "turn_id", "")

    await screen.push(
        session_id,
        {
            "type": "camera",
            "turn_id": turn_id,
            "data": {
                "device": device_name,
                "title": title,
            },
        },
    )
    return {
        "camera": {
            "device": device_name,
            "title": title,
            "status": "streaming",
        }
    }


camera_tool = FunctionTool(camera)
