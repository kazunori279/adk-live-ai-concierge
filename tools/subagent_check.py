"""Check the parts of the Workspace sub-agent's run that used to be ADK's.

`gws._ask` drives its own `Runner` so that a `RunConfig` can be passed per call
— ADK's `AgentTool` builds the `Runner` inside its own `run_async`, where
nothing can reach it. The cost of owning it is that three small pieces of
`agent_tool.py` are now this repo's to get right, and two of them fail quietly:

- **The answer, out of the last content.** Merging a thought part into it is
  silent. The sub-agent thinks in the open — picking a search, reading a
  document — and its reasoning would be read out loud and drawn on the card as
  though it were the answer.
- **The nested run config.** The caller is a BIDI live run, and only the last
  event's content becomes the answer here. Forwarding BIDI unchanged leaves a
  partial chunk as the answer rather than the whole of it, which looks like a
  model that trailed off.
- **The request, marshalled.** The sub-agent's instruction reads named fields
  out of a JSON object, so anything but a bare JSON document is a question it
  reads wrong. Nothing on the far side checks: ADK validates `input_schema`
  only where an agent is a tool, and this is a plain `Runner`, which is exactly
  why the shape has to be got right here.

And the guard in front of all three. `_ask` returns `UNCONFIGURED` without
starting a run at all when there is no `gws` on the machine to drive. It used
to ask only whether the sub-agent had been built, which is never false after
startup, so the message was unreachable: a terminal without the CLI instead
spent a dozen seconds watching the sub-agent retry a subprocess that cannot
exist, and answered in whatever words the model chose. The guard also has to
mean the same thing by `GWS_BIN` as the line that runs the binary does: a bare
name resolved against the working directory rather than through PATH is a
terminal reporting a CLI it has as missing.

And the model it all runs on, which is neither of those things but had nothing
holding it either. Flash over Flash-Lite here is a measured decision that has
been re-argued once and would revert in a one-word edit.

Runs in a fraction of a second, talks to nothing.

    uv run python tools/subagent_check.py

Exits non-zero on the first case that does not hold.
"""

import asyncio
import contextlib
import inspect
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from google.adk import runners  # noqa: E402
from google.adk.agents.context import Context  # noqa: E402
from google.adk.agents.run_config import RunConfig, StreamingMode  # noqa: E402
from google.adk.plugins.base_plugin import BasePlugin  # noqa: E402
from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin  # noqa: E402
from google.adk.sessions.sqlite_session_service import SqliteSessionService  # noqa: E402
from google.adk.telemetry.tracing import trace_call_llm, trace_tool_call  # noqa: E402
from google.adk.tools import agent_tool  # noqa: E402
from google.adk.tools.agent_tool import AgentTool  # noqa: E402
from google.adk.tools.tool_confirmation import ToolConfirmation  # noqa: E402
from google.adk.tools.transfer_to_agent_tool import TransferToAgentTool  # noqa: E402
from google.genai import types  # noqa: E402

from ai_terminal.live.tools import _gws_cli, _gws_run, gws, home  # noqa: E402

# Counted rather than written down, so the closing line cannot drift from the
# number of cases the way a figure typed into a commit message does.
RAN = 0


def check(name: str, got, want, detail: str = "") -> None:
    global RAN
    RAN += 1
    ok = got == want
    print(f"   {'ok  ' if ok else 'FAIL'} {name}: {detail or got}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        sys.exit(1)


@contextlib.contextmanager
def env(**names):
    """Set or unset environment variables for the block, then put them back."""
    kept = {name: os.environ.get(name) for name in names}
    try:
        for name, value in names.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in kept.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def swapped(**attrs):
    """Replace attributes on the `gws` module for the block."""
    kept = {name: getattr(gws, name) for name in attrs}
    try:
        for name, value in attrs.items():
            setattr(gws, name, value)
        yield
    finally:
        for name, value in kept.items():
            setattr(gws, name, value)


def content(*parts: types.Part) -> types.Content:
    return types.Content(role="model", parts=list(parts))


def text(body: str, thought: bool = False) -> types.Part:
    return types.Part(text=body, thought=thought or None)


def main() -> None:
    print("== the answer, out of the last content ==")
    check("one text part is the answer",
          gws._answer(content(text("Two meetings tomorrow.")), None),
          "Two meetings tomorrow.")
    check("two are joined by a newline",
          gws._answer(content(text("Two meetings."), text("- 10:00 standup")),
                      None),
          "Two meetings.\n- 10:00 standup")
    check("a thought is not part of the answer",
          gws._answer(content(text("Search Drive first", thought=True),
                              text("Two meetings.")), None),
          "Two meetings.")
    check("nor is it the answer when it is all there is",
          gws._answer(content(text("Search Drive first", thought=True)), None),
          "")
    check("an empty text part does not become a blank line",
          gws._answer(content(text("A."), text(""), text("B.")), None),
          "A.\nB.")

    print("\n== when there is no answer ==")
    check("no content at all falls back to the error",
          gws._answer(None, "429 RESOURCE_EXHAUSTED"), "429 RESOURCE_EXHAUSTED")
    check("and so does content with no parts",
          gws._answer(types.Content(role="model"), "429 RESOURCE_EXHAUSTED"),
          "429 RESOURCE_EXHAUSTED")
    check("and so does content that was all thought",
          gws._answer(content(text("hm", thought=True)), "429 EXHAUSTED"),
          "429 EXHAUSTED")
    check("with nothing to say and no error, the answer is empty",
          gws._answer(None, None), "")
    # `_ask` turns an empty answer into NO_ANSWER, which is written for the
    # model and named on the card. Returning the error string instead of the
    # text would put a status line where the answer goes.
    check("an error logged mid-run does not beat the answer that followed",
          gws._answer(content(text("Two meetings.")), "429 RESOURCE_EXHAUSTED"),
          "Two meetings.")

    print("\n== the nested run config ==")
    live = RunConfig(streaming_mode=StreamingMode.BIDI,
                     response_modalities=["AUDIO"], max_llm_calls=42)
    nested = gws._nested_config(live)
    check("a BIDI caller runs the sub-agent unary",
          nested.streaming_mode, StreamingMode.NONE)
    check("and is left BIDI itself", live.streaming_mode, StreamingMode.BIDI)
    # The reason for forwarding the caller's config at all rather than building
    # a fresh one: this is the only ceiling on a sub-agent in a loop.
    check("the caller's call ceiling is carried over",
          nested.max_llm_calls, 42)
    check("and the rest of the caller's settings with it",
          [m.value for m in nested.response_modalities], ["AUDIO"])
    cfc = RunConfig(support_cfc=True)
    check("CFC is cleared, because it describes the caller's model",
          gws._nested_config(cfc).support_cfc, False)
    check("and the caller keeps it", cfc.support_cfc, True)
    unary = RunConfig()
    check("a config needing neither is passed through, not copied",
          gws._nested_config(unary) is unary, True)
    check("and no caller config means none to nest",
          gws._nested_config(None), None)

    print("\n== the request, marshalled ==")
    body = gws.GwsRequest(**{"request": "何が入ってる?", "context": "",
                             "lang": "ja"}).model_dump_json(exclude_none=True)
    check("it is a bare JSON document", json.loads(body),
          {"request": "何が入ってる?", "context": "", "lang": "ja"})
    check("with the question in it as itself, not escaped",
          "何が入ってる?" in body, True)
    check("a request on its own still fills the schema",
          json.loads(gws.GwsRequest(request="hi").model_dump_json(
              exclude_none=True)),
          {"request": "hi", "context": "", "lang": ""})
    # The sub-agent's instruction reads these fields by name, so a field the
    # model did not send has to arrive as its default rather than be dropped by
    # `exclude_none`. Nothing on the far side re-parses, which is exactly why
    # this has to hold here.
    check("and no field is dropped on the way",
          set(json.loads(gws.GwsRequest(request="hi").model_dump_json(
              exclude_none=True))),
          {"request", "context", "lang"})

    print("\n== who checks the shape of that document ==")
    # Nobody, and three comments used to say ADK did. The claim is the kind
    # that costs a decision rather than a run: on #34 it read as a hard
    # constraint against appending a sentence after the JSON, which is an
    # option that loses on other grounds and was never ruled out by this one.
    #
    # Pinned in both directions. The prose is read for the wrong claim, and
    # the installed ADK is read for the fact it rests on — a release that
    # started parsing `input_schema` in the Runner would make the comments
    # wrong again, and this is the only thing that would notice.
    source = (Path(gws.__file__).read_text(encoding="utf-8")
              + Path(_gws_run.__file__).read_text(encoding="utf-8"))
    check("nothing claims the far side re-validates the message",
          "re-validate" in source, False)
    # The schema's own docstring, read as the object rather than as a line of
    # the file: it is the one a reader lands on from `input_schema=GwsRequest`,
    # and saying nothing there is how the wrong belief came back last time.
    check("and the schema itself names the path that does validate",
          "agent-as-a-tool" in gws.GwsRequest.__doc__, True)
    check("which is where ADK parses input_schema",
          "input_schema.model_validate"
          in inspect.getsource(agent_tool), True)
    check("and not in the Runner the sub-agent is driven by",
          "input_schema" in inspect.getsource(runners), False)

    print("\n== is there a CLI to drive ==")
    # A directory with a `gws` in it and a directory without, so the answer
    # comes from the fixture rather than from whether this machine happens to
    # have the binary installed.
    with tempfile.TemporaryDirectory() as tmp:
        here = Path(tmp)
        installed = here / "bin"
        installed.mkdir()
        real = installed / "gws"
        real.write_text("#!/bin/sh\nexit 0\n")
        real.chmod(real.stat().st_mode | stat.S_IXUSR)
        unreadable = here / "gws-not-executable"
        unreadable.write_text("#!/bin/sh\nexit 0\n")

        with env(GWS_BIN=None, PATH=str(installed)):
            check("a gws on PATH is one to drive", _gws_cli.available(), True)
            check("and is the one binary() names",
                  _gws_cli.binary(), str(real))
        with env(GWS_BIN=None, PATH=str(here)):
            check("nothing on PATH is nothing to drive",
                  _gws_cli.available(), False)
            # The fallback that makes the check necessary: with nothing found,
            # `binary()` still hands `subprocess.run` a name to fail on.
            check("though binary() still returns a name", _gws_cli.binary(),
                  "gws")
        with env(GWS_BIN=str(real), PATH=str(here)):
            check("GWS_BIN is read, so the two agree on which file",
                  (_gws_cli.available(), _gws_cli.binary()), (True, str(real)))
        with env(GWS_BIN=str(here / "absent"), PATH=str(installed)):
            check("and a GWS_BIN that points nowhere is not rescued by PATH",
                  _gws_cli.available(), False)
        # Existence is not enough: `binary()` hands this path straight to
        # `subprocess.run`, which raises PermissionError rather than running it.
        with env(GWS_BIN=str(unreadable), PATH=str(installed)):
            check("nor is a GWS_BIN that cannot be executed",
                  _gws_cli.available(), False)
        # `binary()` hands its value to `subprocess`, which resolves a bare
        # name through PATH. A guard that resolved the same name against the
        # working directory instead answered that the CLI was not installed on
        # a machine where it was, for the life of the process. So the pair is
        # checked together: one asking whether it can run and the other saying
        # what would be run have to mean the same thing by `GWS_BIN`.
        with env(GWS_BIN="gws", PATH=str(installed)):
            check("a bare GWS_BIN is resolved the way subprocess will resolve "
                  "it", (_gws_cli.available(), _gws_cli.binary()),
                  (True, "gws"))
        with env(GWS_BIN="gws", PATH=str(here)):
            check("and is still nothing to drive when PATH has no gws either",
                  _gws_cli.available(), False)
        # The execute bit on a directory means searchable, so `os.access` says
        # yes to one and there is still nothing to run. The clean card this
        # guard exists to produce beats a GwsError several calls into a
        # question.
        with env(GWS_BIN=str(installed), PATH=str(installed)):
            check("a GWS_BIN naming a directory is not a CLI",
                  _gws_cli.available(), False)

        print("\n== a terminal with no CLI is told so, before anything runs ==")
        called = []

        async def never(args, tool_context):
            called.append(args)
            return "two meetings tomorrow"

        # A sub-agent that was built, which after startup is always the case,
        # so it is the CLI half of the guard being read.
        with swapped(_agent=object(), _run=never):
            with env(GWS_BIN=None, PATH=str(here)):
                answer = asyncio.run(gws._ask({"request": "diary?"}, None))
            check("the answer is the one written for it",
                  answer, gws.UNCONFIGURED)
            check("and the card names it as such",
                  gws._NOT_AN_ANSWER[answer], "unset")
            check("with no run started to find it out", called, [])

            with env(GWS_BIN=None, PATH=str(installed)):
                answer = asyncio.run(gws._ask({"request": "diary?"}, None))
            check("and with a CLI there the question is asked as usual",
                  (answer, called),
                  ("two meetings tomorrow", [{"request": "diary?"}]))

        # The half that was there before and is still worth its token: `_ask`
        # runs inside ADK's background task, where an AttributeError on None is
        # a card that never comes down.
        with swapped(_agent=None, _run=never):
            with env(GWS_BIN=None, PATH=str(installed)):
                check("a sub-agent that was never built is still caught",
                      asyncio.run(gws._ask({"request": "diary?"}, None)),
                      gws.UNCONFIGURED)

    print("\n== which model reads the mailbox ==")
    # A measured choice that nothing pinned. It has been argued twice now: 3.8
    # Flash costs 2.5x the seconds of 3.5 Flash-Lite and is the only one of the
    # two that names the threads instead of counting them, and that stayed true
    # when it was re-run after the instruction fixes meant to rescue Lite. The
    # numbers are in the README; this is the line that would otherwise revert
    # in a one-word edit with nothing to say it had been decided.
    # GOOGLE_CLOUD_LOCATION is set to a region rather than unset, because the
    # mistake below is reading it, and a developer whose shell already says
    # `global` would watch that case pass either way.
    with env(GWS_MODEL=None, GWS_LOCATION=None,
             GOOGLE_CLOUD_LOCATION="us-east1"):
        model = gws.build_gws_agent().canonical_model
        check("the default is 3.8 Flash", model.model, "gemini-3.8-flash")
        # Vertex serves both of these from `global` and 404s everywhere else,
        # so the endpoint cannot follow GOOGLE_CLOUD_LOCATION like its
        # neighbours do. `env_check.py` pins the same split for `browse`.
        check("on the only endpoint serving it",
              model.client_kwargs, {"location": "global"})
    with env(GWS_MODEL="gemini-3.5-flash-lite", GWS_LOCATION="us-east1"):
        model = gws.build_gws_agent().canonical_model
        check("and both are overridable, for the next time it is argued",
              (model.model, model.client_kwargs),
              ("gemini-3.5-flash-lite", {"location": "us-east1"}))

    print("\n== modern ADK (>=2.8.0) prototype compatibility (#585) ==")
    # 1. AgentTool supports skip_summarization for direct sub-agent answers
    gws_agent = gws.build_gws_agent()
    tool = AgentTool(agent=gws_agent, skip_summarization=True)
    check("AgentTool builds with skip_summarization=True",
          tool.skip_summarization, True)
    check("AgentTool exposes inner agent name",
          tool.name, "gws")

    # 2. TransferToAgentTool supports conversational handoffs
    handoff = TransferToAgentTool(agent_names=["gws", "plan"])
    check("TransferToAgentTool registers destination agent names",
          handoff._agent_names, ["gws", "plan"])
    check("TransferToAgentTool exposes standard transfer_to_agent tool name",
          handoff.name, "transfer_to_agent")

    # 3. BasePlugin defines lifecycle callbacks and ReflectAndRetryToolPlugin configures retries
    plugin_callbacks = {
        "before_run_callback", "after_run_callback",
        "before_tool_callback", "after_tool_callback",
        "on_tool_error_callback",
    }
    check("BasePlugin exposes cross-cutting lifecycle hooks",
          plugin_callbacks.issubset(set(dir(BasePlugin))), True)
    retry_plugin = ReflectAndRetryToolPlugin(max_retries=3)
    check("ReflectAndRetryToolPlugin instantiates with retry limit",
          retry_plugin.max_retries, 3)

    # 4. ToolConfirmation supports human-in-the-loop interactive decisions
    confirmation = ToolConfirmation.from_response_dict({"confirmed": True, "hint": "delete event"})
    check("ToolConfirmation parses response dictionary",
          (confirmation.confirmed, confirmation.hint), (True, "delete event"))
    check("Context provides request_confirmation for tool interception",
          hasattr(Context, "request_confirmation"), True)

    # 5. DatabaseSessionService exists in package and SqliteSessionService persists sessions
    import importlib.util  # noqa: PLC0415
    db_spec = importlib.util.find_spec("google.adk.sessions.database_session_service")
    check("DatabaseSessionService module is available in google.adk.sessions",
          db_spec is not None, True)

    async def _test_session_persistence() -> bool:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "check_sess.db"
            service = SqliteSessionService(db_path=str(db_path))
            sess = await service.create_session(app_name="subagent_check", user_id="user_check")
            got = await service.get_session(app_name="subagent_check", user_id="user_check", session_id=sess.id)
            return got is not None and got.id == sess.id

    check("session persistence roundtrips through SQLite session service",
          asyncio.run(_test_session_persistence()), True)

    # 6. Observability: telemetry tracing functions are available
    check("ADK telemetry tracing functions are callable",
          callable(trace_call_llm) and callable(trace_tool_call), True)

    print("\n== google home mcp client and read-only sub-agent tool wiring (#744) ==")
    # 1. Unconfigured behavior when credentials file is missing
    with tempfile.TemporaryDirectory() as empty_dir:
        with env(HOME_CONFIG_DIR=empty_dir):
            home.close_home_toolset()

            async def _check_missing_creds():
                try:
                    await home.get_access_token()
                    return False
                except home.HomeAuthError:
                    return True

            check("missing credentials raises HomeAuthError",
                  asyncio.run(_check_missing_creds()), True)
            res = asyncio.run(home.home("turn on living room lights"))
            check("missing credentials returns UNCONFIGURED gracefully",
                  res.get("answer"), home.UNCONFIGURED)

    # 2. Token refresh headers, TTL caching, and auth error handling
    with tempfile.TemporaryDirectory() as cred_dir:
        c_file = Path(cred_dir) / "home-mcp-client.json"
        t_file = Path(cred_dir) / "home-mcp-token.json"
        c_file.write_text(json.dumps({"installed": {"client_id": "test_id", "client_secret": "test_sec"}}))
        t_file.write_text(json.dumps({"refresh_token": "test_refresh"}))

        with env(HOME_CONFIG_DIR=cred_dir):
            home.close_home_toolset()

            async def _mock_post_err(self, url, **kwargs):
                return httpx.Response(status_code=400, text="Bad Request")

            orig_post = httpx.AsyncClient.post
            httpx.AsyncClient.post = _mock_post_err
            try:
                got_err = False
                try:
                    asyncio.run(home.get_access_token(force_refresh=True))
                except home.HomeAuthError:
                    got_err = True
                check("token refresh failure raises HomeAuthError", got_err, True)
            finally:
                httpx.AsyncClient.post = orig_post

            captured_refresh_data = {}

            async def _mock_post_ok(self, url, data=None, **kwargs):
                if "oauth2.googleapis.com" in str(url):
                    captured_refresh_data.update(data or {})
                    return httpx.Response(
                        status_code=200,
                        json={"access_token": "mock_token_123", "expires_in": 3600},
                    )
                return httpx.Response(status_code=404)

            httpx.AsyncClient.post = _mock_post_ok
            try:
                tok = asyncio.run(home.get_access_token(force_refresh=True))
                check("token refresh posts grant_type", captured_refresh_data.get("grant_type"), "refresh_token")
                check("token refresh posts client_id", captured_refresh_data.get("client_id"), "test_id")
                check("token refresh posts client_secret", captured_refresh_data.get("client_secret"), "test_sec")
                check("token refresh posts refresh_token", captured_refresh_data.get("refresh_token"), "test_refresh")
                check("token refreshed successfully", tok, "mock_token_123")

                # In-memory access token cache with TTL
                tok2 = asyncio.run(home.get_access_token(force_refresh=False))
                check("valid token returns from in-memory cache without refresh", tok2, "mock_token_123")

                # Fast-forward expires_at to within 5-minute pre-expiry margin (300s)
                home._token_cache["expires_at"] = time.time() + 200

                async def _mock_post_refreshed(self, url, data=None, **kwargs):
                    return httpx.Response(
                        status_code=200,
                        json={"access_token": "refreshed_token_456", "expires_in": 3600},
                    )

                httpx.AsyncClient.post = _mock_post_refreshed
                tok3 = asyncio.run(home.get_access_token(force_refresh=False))
                check("token refreshes within 5 minutes of expiry", tok3, "refreshed_token_456")
            finally:
                httpx.AsyncClient.post = orig_post

    # 3. Structure resolution and structured_content output extraction
    home.close_home_toolset()
    captured_mcp_calls = []

    async def _mock_mcp_post(self, url, headers=None, json=None, **kwargs):
        call_params = json.get("params", {}) if json else {}
        name = call_params.get("name")
        args = call_params.get("arguments", {})
        captured_mcp_calls.append((name, dict(args)))
        if name == "list_homes":
            return httpx.Response(
                status_code=200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [{"type": "text", "text": "{}"}],
                        "structured_content": {
                            "structures": [{"id": "structure-123", "name": "Living Home"}]
                        },
                    },
                },
            )
        elif name in ("list_home_resources", "list_home_states", "list_home_history"):
            return httpx.Response(
                status_code=200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "content": [{"type": "text", "text": "{}"}],
                        "structured_content": {
                            "devices": [{"id": "light-1", "name": "Desk Lamp", "state": "on"}]
                        },
                    },
                },
            )
        return httpx.Response(status_code=404)

    orig_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = _mock_mcp_post
    try:
        home._token_cache = {"access_token": "valid_token", "expires_at": time.time() + 3600}
        homes = asyncio.run(home.list_homes())
        check("list_homes extracts structured_content",
              "structures" in homes and homes["structures"][0]["id"] == "structure-123", True)
        check("list_homes caches structureId",
              home.get_cached_structure_id(), "structure-123")

        # Calling list_home_states without explicit structureId injects cached structureId
        states = asyncio.run(home.list_home_states())
        check("list_home_states auto-injects cached structureId",
              captured_mcp_calls[-1], ("list_home_states", {"structureId": "structure-123"}))
        check("list_home_states extracts structured payload",
              states.get("devices", [{}])[0].get("state"), "on")
    finally:
        httpx.AsyncClient.post = orig_post

    # 4. Response scheduling & Agent tool wrapping
    home_tool = home.build_home_tool()
    check("home tool response scheduling is INTERRUPT",
          home_tool.response_scheduling, types.FunctionResponseScheduling.INTERRUPT)
    check("home tool is a FunctionTool named home",
          home_tool.name, "home")

    agent = home.build_home_agent()
    check("home sub-agent equips 5 tools including run_home_actions",
          [t.name for t in agent.tools],
          ["list_homes", "list_home_resources", "list_home_states", "list_home_history", "run_home_actions"])

    # 5. Cleanup lifecycle
    home.set_cached_structure_id("temp-structure")
    home._token_cache = {"access_token": "temp_tok", "expires_at": 999999.0}
    home.close_home_toolset()
    check("close_home_toolset clears cached structureId",
          home.get_cached_structure_id(), None)
    check("close_home_toolset clears token cache",
          home._token_cache.get("access_token"), None)

    # 6. Sub-agent runner execution and error handling (#744)
    orig_run_async = home.Runner.run_async
    orig_close = home.Runner.close
    runner_closed = False

    async def _mock_close(self):
        nonlocal runner_closed
        runner_closed = True
        return await orig_close(self)

    async def _mock_run_async(self, *args, **kwargs):
        yield type(
            "Event",
            (),
            {
                "error_message": None,
                "content": types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Living room light is on.")],
                ),
            },
        )()

    home.Runner.run_async = _mock_run_async
    home.Runner.close = _mock_close
    try:
        res = asyncio.run(home._run_home_subagent("is the living room light on?"))
        check("home sub-agent runner yields answer via Aclosing",
              res.get("answer"), "Living room light is on.")
        check("home sub-agent runner cleans up runner on completion",
              runner_closed, True)
    finally:
        home.Runner.run_async = orig_run_async
        home.Runner.close = orig_close

    # 7. Living room scope resolution, device action execution, and fail-closed refusal (#746)
    print("\n== google home living room device control & scope guard (#746) ==")
    mock_resources_data = {
        "rooms": [
            {"id": "room-living", "displayName": "リビング", "devices": ["shade-1", {"id": "plug-ac", "displayName": "エアコン"}]},
            {"id": "room-bedroom", "displayName": "寝室", "devices": [{"id": "ac-bedroom", "displayName": "寝室のエアコン"}]},
            {"id": "room-other", "displayName": "その他", "devices": [{"id": "thermo-living", "displayName": "リビングのエアコン"}]},
        ],
        "devices": [
            {"id": "shade-1", "displayName": "Smart Roller shade", "room": "room-living"},
            {"id": "plug-ac", "displayName": "エアコン", "room": "room-living"},
            {"id": "thermo-living", "displayName": "リビングのエアコン", "room": "room-other"},
            {"id": "ac-bedroom", "displayName": "寝室のエアコン", "room": "room-bedroom"},
            {"id": "light-bedroom3", "displayName": "寝室3のライト", "room": "寝室3"},
            {"id": "device@uuid-shade", "displayName": "リビングシェード", "parentIds": ["room-living"], "type": "WindowCoveringDevice"},
            {"id": "device@uuid-ac-bed", "displayName": "寝室エアコン", "parentIds": ["room-bedroom"], "type": "ThermostatDevice"},
        ],
    }

    check("extract_device_room_map handles non-dict input",
          home.extract_device_room_map("error string"), {})
    room_map = home.extract_device_room_map(mock_resources_data)
    check("shade-1 resolves to リビング", room_map.get("shade-1"), "リビング")
    check("Smart Roller shade resolves to リビング", room_map.get("Smart Roller shade"), "リビング")
    check("エアコン resolves to リビング", room_map.get("エアコン"), "リビング")
    check("thermo-living resolves to その他", room_map.get("thermo-living"), "その他")
    check("ac-bedroom resolves to 寝室", room_map.get("ac-bedroom"), "寝室")
    check("light-bedroom3 resolves to 寝室3", room_map.get("light-bedroom3"), "寝室3")
    check("parentIds resolves living room device", room_map.get("device@uuid-shade"), "リビング")
    check("parentIds resolves bedroom device", room_map.get("device@uuid-ac-bed"), "寝室")

    captured_action_mcp_calls = []

    async def _mock_action_mcp_post(self, url, headers=None, json=None, **kwargs):
        call_params = json.get("params", {}) if json else {}
        name = call_params.get("name")
        args = call_params.get("arguments", {})
        captured_action_mcp_calls.append((name, dict(args)))
        if name == "list_home_resources":
            return httpx.Response(
                status_code=200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [{"type": "text", "text": "{}"}],
                        "structured_content": mock_resources_data,
                    },
                },
            )
        elif name == "run_home_actions":
            return httpx.Response(
                status_code=200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "content": [{"type": "text", "text": "{}"}],
                        "structured_content": {"status": "SUCCESS"},
                    },
                },
            )
        return httpx.Response(status_code=404)

    orig_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = _mock_action_mcp_post
    try:
        home._token_cache = {"access_token": "valid_token", "expires_at": time.time() + 3600}
        home.set_cached_structure_id("struct-living-456")
        # Allowed living room control
        res_living = asyncio.run(
            home.run_home_actions(actions=[{"deviceId": "shade-1", "commands": [{"open": {}}]}])
        )
        check("living room shade control succeeds", res_living.get("status"), "SUCCESS")
        check("run_home_actions auto-injects structureId",
              captured_action_mcp_calls[-1][1].get("structureId"), "struct-living-456")

        # Allowed living room control with homeActionRequests
        captured_action_mcp_calls.clear()
        res_direct = asyncio.run(
            home.run_home_actions(
                homeActionRequests=[
                    {"id": "device@uuid-shade", "type": "WindowCoveringDevice", "command": "WindowCovering.SetPosition"}
                ]
            )
        )
        check("run_home_actions with homeActionRequests succeeds", res_direct.get("status"), "SUCCESS")
        check("run_home_actions calls MCP with homeActionRequests",
              "homeActionRequests" in captured_action_mcp_calls[-1][1] and "actions" not in captured_action_mcp_calls[-1][1], True)

        # Scope guard refusal: その他 device
        captured_action_mcp_calls.clear()
        res_other = asyncio.run(
            home.run_home_actions(actions=[{"deviceId": "thermo-living", "commands": [{"setpoint": 24}]}])
        )
        check("その他 device is refused", res_other.get("refused"), True)
        check("その他 device refusal message is exact",
              res_other.get("error"), "リビング以外の機器は操作できません")

        # Scope guard refusal: mixed living room and non-living room request
        captured_action_mcp_calls.clear()
        res_mixed = asyncio.run(
            home.run_home_actions(
                homeActionRequests=[
                    {"id": "device@uuid-shade"},
                    {"id": "device@uuid-ac-bed"},
                ]
            )
        )
        check("mixed request is refused", res_mixed.get("refused"), True)
        check("mixed request refusal message is exact", res_mixed.get("error"), "リビング以外の機器は操作できません")
        check("run_home_actions MCP was NOT called for mixed request",
              any(c[0] == "run_home_actions" for c in captured_action_mcp_calls), False)

        # Fail-closed refusal for missing or unresolvable device id
        res_missing_id = asyncio.run(home.run_home_actions(homeActionRequests=[{"type": "WindowCoveringDevice"}]))
        check("missing device id returns fail-closed refusal", res_missing_id.get("refused"), True)
        check("missing device id refusal error is exact", res_missing_id.get("error"), "リビング以外の機器は操作できません")

        res_unresolvable = asyncio.run(home.run_home_actions(homeActionRequests=[{"id": "unknown-nonexistent-device"}]))
        check("unresolvable device id returns fail-closed refusal", res_unresolvable.get("refused"), True)

        # Refusals for bedroom, unlisted devices, and suppression of MCP tool calls
        res_bed = asyncio.run(home.run_home_actions(actions=[{"deviceId": "ac-bedroom"}]))
        res_unlisted = asyncio.run(home.run_home_actions(actions=[{"deviceId": "unknown-device-999"}]))
        assert res_bed.get("refused") is True and res_bed.get("error") == "リビング以外の機器は操作できません"
        assert res_unlisted.get("refused") is True and res_unlisted.get("error") == "リビング以外の機器は操作できません"

        check("run_home_actions MCP was NOT called for その他 device",
              any(c[0] == "run_home_actions" for c in captured_action_mcp_calls), False)
    finally:
        httpx.AsyncClient.post = orig_post
        home.close_home_toolset()

    # 8. Google Home status summary and timeline cards (#745)
    print("\n== google home status summary card and timeline card payloads (#745) ==")
    mock_summary_states = {
        "devices": [
            {"id": "d1", "displayName": "リビング照明", "state": "on", "room": "リビング"},
            {"id": "d2", "displayName": "エアコン", "state": "on", "detail": "25°C", "room": "リビング"},
            {"id": "d3", "displayName": "寝室エアコン", "state": "off", "room": "寝室"},
            {"id": "d4", "displayName": "玄関ライト", "on": True},  # No room in state, fallback
        ]
    }
    mock_summary_resources = {
        "resources": [
            {"id": "d4", "name": "玄関ライト", "room": "玄関"},
        ]
    }
    summary_payload = getattr(home, "build_home_summary_payload", lambda s, r=None: {})(mock_summary_states, mock_summary_resources)
    check("summary payload type is home", summary_payload.get("type"), "home")
    check("summary payload card_type is summary", summary_payload.get("card_type"), "summary")
    rooms = summary_payload.get("rooms", [])
    check("summary payload groups into 3 rooms", len(rooms), 3)
    living_room = next((r for r in rooms if r["name"] == "リビング"), None)
    check("living room contains 2 devices", len(living_room["devices"]) if living_room else 0, 2)
    dev_ac = next((d for d in living_room["devices"] if d["name"] == "エアコン"), {}) if living_room else {}
    check("device detail preserved in summary", dev_ac.get("detail"), "25°C")
    genkan_room = next((r for r in rooms if r["name"] == "玄関"), None)
    check("resource room mapping resolved d4 to 玄関", bool(genkan_room and len(genkan_room["devices"]) == 1), True)

    mock_history_data = {
        "events": [
            {"timestamp": "14:00", "deviceName": "玄関ドア", "description": "開扉検知"},
            {"time": "14:05", "device": "リビング照明", "event": "点灯"},
        ]
    }
    timeline_payload = getattr(home, "build_home_timeline_payload", lambda h: {})(mock_history_data)
    check("timeline payload type is home", timeline_payload.get("type"), "home")
    check("timeline payload card_type is timeline", timeline_payload.get("card_type"), "timeline")
    events = timeline_payload.get("events", [])
    check("timeline payload formats 2 events", len(events), 2)
    check("timeline event fields mapped", events[0] if events else {}, {"time": "14:00", "device": "玄関ドア", "event": "開扉検知"})
    check("timeline event alternate fields mapped", events[1] if len(events) > 1 else {}, {"time": "14:05", "device": "リビング照明", "event": "点灯"})

    # Test out-of-band screen push and transient action suppression
    captured_screen_pushes = []

    async def _mock_screen_push(session_id, payload):
        captured_screen_pushes.append((session_id, payload))

    screen_mod = getattr(home, "screen", None)
    orig_screen_push = getattr(screen_mod, "push", None) if screen_mod else None
    if screen_mod:
        screen_mod.push = _mock_screen_push
    try:
        class MockSession:
            id = "test-session-745"

        class MockToolContext:
            session = MockSession()
            session_id = "test-session-745"

        ctx = MockToolContext()

        # Mock subagent run that sets collector states
        async def _mock_run_subagent_states(*args, **kwargs):
            collector = getattr(home, "_HOME_COLLECTOR", None)
            if collector and (col := collector.get()) is not None:
                col["states"] = mock_summary_states
                col["resources"] = mock_summary_resources
            return {"answer": "家全体の様子です。"}

        orig_run_sub = home._run_home_subagent
        home._run_home_subagent = _mock_run_subagent_states
        home._token_cache = {"access_token": "valid_token", "expires_at": time.time() + 3600}

        captured_screen_pushes.clear()
        res = asyncio.run(home.home("家の様子は？", tool_context=ctx))
        check("subagent with states returns answer", res.get("answer"), "家全体の様子です。")
        check("subagent with states pushes summary card to screen",
              len(captured_screen_pushes) == 1 and captured_screen_pushes[0][1].get("card_type") == "summary", True)

        # Mock subagent run that sets collector history
        async def _mock_run_subagent_history(*args, **kwargs):
            collector = getattr(home, "_HOME_COLLECTOR", None)
            if collector and (col := collector.get()) is not None:
                col["history"] = mock_history_data
            return {"answer": "履歴です。"}

        home._run_home_subagent = _mock_run_subagent_history
        captured_screen_pushes.clear()
        res_h = asyncio.run(home.home("履歴を見せて", tool_context=ctx))
        check("subagent with history pushes timeline card to screen",
              len(captured_screen_pushes) == 1 and captured_screen_pushes[0][1].get("card_type") == "timeline", True)

        # Mock transient action: no states or history in collector
        async def _mock_run_subagent_action(*args, **kwargs):
            return {"answer": "リビングの照明をつけました。"}

        home._run_home_subagent = _mock_run_subagent_action
        captured_screen_pushes.clear()
        res_a = asyncio.run(home.home("照明をつけて", tool_context=ctx))
        check("transient simple action does NOT push card to screen", len(captured_screen_pushes), 0)
    finally:
        if screen_mod and orig_screen_push:
            screen_mod.push = orig_screen_push
        home._run_home_subagent = orig_run_sub

    print(f"\nall {RAN} cases hold")


if __name__ == "__main__":
    main()
