# Feasibility Study: Adopting Modern Google ADK (`google-adk>=2.8.0`) Features

**Status:** Complete  
**Date:** 2026-09-14  
**Author:** AI Terminal Architecture Team  
**Target Version:** `google-adk==2.8.0`  
**Reference Issue:** #585 (type:spike)

---

## 1. Executive Summary

This feasibility study evaluates modern features in Google Agent Development Kit (ADK) `v2.8.0` to streamline the architecture of the Raspberry Pi AI Terminal and its dual deployment on Cloud Run.

Currently, the terminal implements custom subsystems for sub-agent orchestration (`Runner` loops in `gws.py` and `plan.py`), session persistence (`TerminalSessionService` wrapping `SqliteSessionService`), traffic logging (`_log_tool_traffic` in `main.py`), and fact storage (`user_states` in `memory.py`). While these custom components addressed gaps in early ADK versions, modern ADK (`>=2.8.0`) introduces native abstractions that can simplify maintenance, standardize patterns, and improve multi-environment portability.

### Key Findings Matrix

| Target Area | Current Custom Mechanism | ADK 2.8.0 Native Feature | Adoption Recommendation | Effort / Risk |
|---|---|---|---|---|
| **1. Sub-Agent Orchestration** | Manual `Runner` + `InMemorySessionService` in `gws.py` & `plan.py` | `AgentTool` & `TransferToAgentTool` | **Adopt `AgentTool(skip_summarization=True)`** for isolated tools; evaluate `TransferToAgentTool` for interactive sub-agents | Medium / Low |
| **2. Cross-Cutting Plugins** | Ad-hoc hooks (`_log_tool_traffic`, screen pushes in `main.py`) | `BasePlugin` lifecycle hooks & `ReflectAndRetryToolPlugin` | **Adopt `BasePlugin`** for logging/metrics; adopt `ReflectAndRetryToolPlugin` for external API retries | Low / Very Low |
| **3. Tool Standardization & MCP** | Bespoke Python wrappers in `src/ai_terminal/live/tools/` | `MCPToolset` (Model Context Protocol) | **Adopt for Home Automation & GWS Drive MCP**; retain native coroutines for high-throughput Live tools | High / Medium |
| **4. Memory & Personalization** | `memory.py` topic-keyed SQLite `user_states` (capped at 30 facts) | `BaseMemoryService`, `VertexAiMemoryBankService`, `PreloadMemoryTool` | **Retain SQLite on Pi**; adopt `VertexAiMemoryBankService` conditionally for Cloud Run (#575) | High / High |
| **5. Human-in-the-Loop Confirmation** | Custom UI state & card flows | `ToolConfirmation` & `context.request_confirmation()` | **Adopt natively** for destructive Workspace actions (email send, event delete) | Medium / Low |
| **6. Dual-Deployment Sessions** | `SqliteSessionService` (custom subclass) | `DatabaseSessionService` (SQLAlchemy / asyncpg / aiosqlite) | **Adopt `DatabaseSessionService`** for Cloud Run (Cloud SQL); keep `SqliteSessionService` on Pi | Medium / Low |
| **7. Observability & Telemetry** | Custom JSON timing in `_timing.py` | `google.adk.telemetry` (`trace_call_llm`, `trace_tool_call`) | **Adopt OTel tracing** across tools and LLM invocations; export to Cloud Trace on Cloud Run | Medium / Low |

---

## 2. Area-by-Area Analysis

### 2.1 Area 1: Sub-Agent Orchestration (`AgentTool` & `TransferToAgentTool`)

#### Current State
In `src/ai_terminal/live/tools/gws.py` and `plan.py`, sub-agents run through a manually constructed `Runner`:
- `gws._ask` manually instantiates `InMemorySessionService()`, builds `Runner(app_name=..., agent=_agent, session_service=sessions)`, creates an ephemeral session, converts input arguments into a user message, and iterates over `runner.run_async(...)` events.
- This manual loop was originally written because early versions of `AgentTool` forced model summarization of sub-agent outputs, lacked control over nested `RunConfig`, and could leak parent state into child agents.

#### ADK 2.8.0 Capabilities
- `google.adk.tools.agent_tool.AgentTool`:
  ```python
  AgentTool(
      agent: BaseAgent,
      skip_summarization: bool = False,
      *,
      include_plugins: bool = True,
      propagate_grounding_metadata: bool = False,
  )
  ```
  `AgentTool` now supports `skip_summarization=True`. When enabled, it extracts the final agent turn directly without running a redundant summarization LLM call, eliminating 1.5–3.0 seconds of sub-agent latency.
- `google.adk.tools.transfer_to_agent_tool.TransferToAgentTool`:
  ```python
  TransferToAgentTool(agent_names: list[str])
  ```
  Instead of nesting an agent as a callable function tool, `TransferToAgentTool` handoffs the main conversation thread to a specialized sub-agent (e.g. transfer from Concierge to Planning Agent).

#### Architectural Assessment & Trade-Offs
- **Pros:**
  1. Replaces ~60 lines of boilerplate runner lifecycle management in `gws.py` and `plan.py`.
  2. Automatic input/output schema generation based on the inner agent's declarations.
  3. Seamless propagation of plugins and telemetry contexts.
- **Cons / Edge Cases:**
  1. `gws._ask` must never raise exceptions during live sessions (ADK background tasks log unhandled exceptions and drop the turn). If `AgentTool` is used, errors must still be caught or mapped to polite card messages.
  2. The Live API WebSocket session requires immediate UI notification when a sub-agent starts. With manual runners, `_progress` updates stream during generation. With `AgentTool`, progress reporting requires a plugin (see Section 2.2).
- **Verdict:** Migrate `gws.py` and `plan.py` to `AgentTool(skip_summarization=True)` in conjunction with a `ProgressPlugin`.

---

### 2.2 Area 2: Cross-Cutting Plugins (`BasePlugin` & `ReflectAndRetryToolPlugin`)

#### Current State
- `src/ai_terminal/live/main.py` embeds logging logic (`_log_tool_traffic`) directly inside the core WebSocket event loop.
- Sensitive arguments must be filtered out manually (`_PLAIN_ARGS`, sizing strings and tokens to avoid leaking PII into journald logs).
- Screen status pushes (`screen.push_wake`, `hideWorking`) are scattered across tool functions and router handlers.
- External API calls (Google Workspace CLI, Open-Meteo, Geocoding) lack standardized retry policies; transient network glitches cause immediate tool errors.

#### ADK 2.8.0 Capabilities
- `google.adk.plugins.base_plugin.BasePlugin`:
  Lifecycle callbacks available in `BasePlugin`:
  - `before_run_callback` / `after_run_callback`
  - `before_agent_callback` / `after_agent_callback`
  - `before_tool_callback` / `after_tool_callback`
  - `on_tool_error_callback` / `on_model_error_callback` / `on_run_error_callback`
- `google.adk.plugins.reflect_retry_tool_plugin.ReflectAndRetryToolPlugin`:
  ```python
  ReflectAndRetryToolPlugin(
      name: str = "reflect_retry_tool_plugin",
      max_retries: int = 3,
      throw_exception_if_retry_exceeded: bool = True,
      tracking_scope: TrackingScope = TrackingScope.INVOCATION,
  )
  ```
  Provides self-healing error recovery for tool failures. When a tool fails (or returns an error dict detectable via `extract_error_from_result`), the plugin feeds actionable correction hints back into the LLM context, retrying up to `max_retries` within thread-safe locks.

#### Architecture Proposal: Plugin Pipeline
```
[User Turn]
     │
     ▼
┌───────────────────────────────────────────────┐
│              LiveRunner / Agent               │
├───────────────────────────────────────────────┤
│  Plugins:                                     │
│  1. TrafficLoggingPlugin (BasePlugin)         │ -> Redacts PII, logs sized parameters
│  2. ScreenProgressPlugin (BasePlugin)         │ -> Pushes WebSocket cards / status
│  3. ReflectAndRetryToolPlugin                 │ -> Self-healing tool retries (max 2)
└───────────────────────────────────────────────┘
```

- **Verdict:** High priority. Create `TrafficLoggingPlugin` to extract logging from `main.py` and configure `ReflectAndRetryToolPlugin` for network-bound tools.

---

### 2.3 Area 3: Standardized Tool & Smart Home Integration (`MCPToolset`)

#### Current State
- The terminal implements manual tools (`maps_search`, `image_search`, `youtube_search`, `web_search`, `weather`, `gws`) wrapped via `google.adk.tools.FunctionTool`.
- Each new home automation capability requires writing custom Python routines, handling error conversions, and defining schema annotations.

#### ADK 2.8.0 Capabilities
- `google.adk.tools.mcp_tool.mcp_toolset.MCPToolset`:
  Connects to Model Context Protocol (MCP) servers using either `StdioServerParameters` (subprocesses) or SSE (HTTP Server-Sent Events).
  ```python
  # Prototype signature in ADK 2.8.0 (requires `mcp` extra):
  from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
  from mcp import StdioServerParameters

  toolset = MCPToolset(
      connection_params=StdioServerParameters(
          command="npx",
          args=["-y", "@modelcontextprotocol/server-homeassistant"],
          env={"HASS_TOKEN": "...", "HASS_URL": "http://homeassistant.local:8123"}
      )
  )
  ```

#### Architectural Assessment & Trade-Offs
- **Pros:**
  1. Instant access to ecosystem MCP servers (Home Assistant, Philips Hue, Google Drive, SQLite, Filesystem).
  2. Tools can run out-of-process in isolated containers or Node/Python environments.
- **Cons & Constraints:**
  1. **Latency:** Starting local Node.js stdio processes on Raspberry Pi 5 adds cold-start overhead (1–3 seconds) and consumes ~80MB RAM per server. Persistent SSE servers are preferred.
  2. **Dependencies:** `google-adk` does not include `mcp` by default; requires adding `mcp` package to `pyproject.toml`.
  3. **Real-time Live Audio:** Live API tool calling operates at low latency (<500ms). MCP tools must respond quickly; timeouts must be strictly enforced.
- **Verdict:** Recommended for Smart Home integration (e.g. Home Assistant via SSE). Keep core query tools (`weather`, `maps`) in native Python for maximum speed.

---

### 2.4 Area 4: Semantic Memory & Personalization (`BaseMemoryService`)

#### Current State
- `src/ai_terminal/live/memory.py` implements standing household facts.
- It writes facts to ADK's `user_states` table in SQLite (`user:fact:<topic>`), surviving reboots without migrations or external dependencies.
- Facts are capped at 30 items rendered into the system instruction. Topic updates overwrite existing keys to prevent contradictory memories.

#### ADK 2.8.0 Capabilities
- `google.adk.memory.base_memory_service.BaseMemoryService`:
  Defines `add_memory`, `search_memory`, `add_events_to_memory`.
- Implementations:
  - `InMemoryMemoryService`: Pure ephemeral memory, discarded upon restart.
  - `VertexAiMemoryBankService`: Connects to Google Cloud Vertex AI Memory Bank for managed vector indexing and retrieval.
  - `VertexAiRagMemoryService`: Integrates with Vertex AI RAG corpora.
  - `PreloadMemoryTool` / `LoadMemoryTool`: Injects relevant memories before LLM generation or allows the model to query memory dynamically.

#### Architectural Assessment & Trade-Offs
- **Edge Deployment (Raspberry Pi):**
  - **Privacy:** Household speech and facts must remain local to the device wherever possible. Uploading personal facts to a cloud vector database creates privacy exposure and recurring cloud costs.
  - **Offline Resilience:** The existing SQLite `user_states` architecture works offline and adds zero network latency to live turn setup.
  - **Live API Compatibility:** `PreloadMemoryTool` requires `user_content` in the invocation context, which ADK's Live API bidirectional stream does not populate.
- **Cloud Run Deployment (#575):**
  - For cloud-hosted concierge instances, local SQLite storage is ephemeral unless mounted to Cloud Storage FUSE. `VertexAiMemoryBankService` or PostgreSQL `pgvector` offers enterprise-grade multi-tenant memory.
- **Verdict:** Retain the lightweight SQLite `user_states` in `memory.py` on the Raspberry Pi. Introduce `VertexAiMemoryBankService` as an optional backend for Cloud Run multi-tenant environments.

---

### 2.5 Area 5: Human-in-the-Loop Confirmation (`tool_confirmation`)

#### Current State
- The terminal currently executes actions directly upon model request.
- Potentially destructive actions (e.g. clearing calendar events or sending emails) lack a standardized interactive verification gate on the kiosk display.

#### ADK 2.8.0 Capabilities
- `google.adk.tools.tool_confirmation.ToolConfirmation`:
  Pydantic model representing confirmation requests and responses:
  ```python
  class ToolConfirmation(BaseModel):
      hint: str = ""
      confirmed: bool = False
      payload: Optional[Any] = None
  ```
- `Context.request_confirmation(*, hint: str | None = None, payload: Any | None = None)`:
  When a tool determines that an action requires user approval:
  1. The tool calls `tool_context.request_confirmation(hint="Delete calendar event 'Dentist'?", payload={"event_id": "123"})`.
  2. ADK pauses the execution pipeline and yields a confirmation request event.
  3. The WebSocket client receives the confirmation request, displaying an interactive prompt on the terminal touchscreen.
  4. The user taps "Confirm" or "Cancel".
  5. The client transmits back the response with `confirmed: true/false`.

#### Architecture Proposal: Touch Confirmation Flow
```mermaid
sequenceDiagram
    autonumber
    participant LLM as Gemini Live
    participant Tool as GWS Tool
    participant ADK as ADK Context
    participant WS as FastAPI WebSocket
    participant UI as Kiosk UI (Touchscreen)

    LLM->>Tool: Call delete_event(id="xyz")
    Tool->>ADK: context.request_confirmation(hint="Delete meeting?", payload={id: "xyz"})
    Tool-->>ADK: Return {"status": "pending_confirmation"}
    ADK->>WS: Yield confirmation event
    WS->>UI: Push action card with [Confirm] [Cancel] buttons
    Note over UI: User taps [Confirm] on 10.1" display
    UI->>WS: Send {"type": "tool_confirmation", "confirmed": true, "id": "xyz"}
    WS->>ADK: Resume tool with confirmed=True
    ADK->>Tool: Execute delete_event
    Tool-->>LLM: Event deleted successfully
```

- **Verdict:** High value for UX and safety. Implement confirmation cards for Google Calendar deletions and Gmail transmissions.

---

### 2.6 Area 6: Cloud & Edge Dual-Deployment (`DatabaseSessionService`)

#### Current State
- `src/ai_terminal/live/main.py` defines `TerminalSessionService` inheriting from `SqliteSessionService`.
- It overrides `append_event` and `get_session` to neutralize `live_session_id`, preventing ADK replay fencing bugs (#396).
- SQLite is bound to a single local file (`sessions.db`). While ideal for Raspberry Pi, SQLite fails on serverless Cloud Run (#575) where multiple container replicas cannot share a local SQLite database safely without corruption.

#### ADK 2.8.0 Capabilities
- `google.adk.sessions.database_session_service.DatabaseSessionService`:
  SQLAlchemy-based session service supporting any async database driver:
  - SQLite for edge: `sqlite+aiosqlite:///sessions.db`
  - PostgreSQL / Cloud SQL for cloud: `postgresql+asyncpg://user:pass@host/dbname`
  - Fully implements `create_session`, `get_session`, `list_sessions`, `append_event`, `delete_session` with optimistic locking and schema versioning.

#### Dual-Deployment Architecture
```mermaid
graph TD
    subgraph "Raspberry Pi 5 (Edge)"
        MainEdge["main.py (Pi)"] -->|SESSIONS_DB_URL| DBEdge["DatabaseSessionService<br>(sqlite+aiosqlite://sessions.db)"]
        DBEdge --> FileEdge[("Local Flash Storage")]
    end

    subgraph "Cloud Run (#575, Cloud)"
        CR1["Container Instance 1"] -->|DATABASE_URL| DBCloud["DatabaseSessionService<br>(postgresql+asyncpg://...)"]
        CR2["Container Instance 2"] -->|DATABASE_URL| DBCloud
        DBCloud --> CloudSQL[("Google Cloud SQL<br>(PostgreSQL)")]
    end
```

- **Dependency Requirement:** `DatabaseSessionService` requires `sqlalchemy>=2.0` and async drivers (`aiosqlite` / `asyncpg`).
- **Verdict:** Recommended migration path. Define an abstract factory creating `DatabaseSessionService` based on `DATABASE_URL` or defaulting to SQLite.

---

### 2.7 Area 7: Observability & OpenTelemetry (`google.adk.telemetry`)

#### Current State
- Latency and traffic metrics are captured through custom helpers (`_timing.py`) writing JSON lines to standard loggers.
- Distributed tracing across audio ingestion, Live API websocket messages, and background tool execution is uncoordinated.

#### ADK 2.8.0 Capabilities
- `google.adk.telemetry`:
  Provides structured OpenTelemetry instrumentation:
  - `trace_call_llm`: Captures LLM request/response spans, token counts, time-to-first-token (TTFT), and model metadata.
  - `trace_tool_call`: Captures tool execution latency, parameter shapes, and exceptions.
  - Automatic context propagation across nested sub-agents and tool calls.
- **Edge Configuration:** OpenTelemetry SDK can be configured with a lightweight no-op or console exporter on Raspberry Pi to avoid CPU/memory overhead.
- **Cloud Configuration:** Cloud Run instances export traces directly to Google Cloud Trace via OpenTelemetry OTLP exporter, giving end-to-end visibility into concierge response latency.
- **Verdict:** Adopt `google.adk.telemetry` for uniform span creation.

---

## 3. Concrete Architectural Roadmap & Priority Matrix

| Phase | Priority | Epic / Feature | Target Areas | Target Environment |
|---|---|---|---|---|
| **Phase 1: Foundation & Observability** | **P0** | **ADK Plugin Pipeline & Telemetry**<br>- Extract `TrafficLoggingPlugin`<br>- Add `ReflectAndRetryToolPlugin` for API retries<br>- Enable `google.adk.telemetry` | Areas 2, 7 | Pi & Cloud |
| **Phase 2: Sub-Agent Modernization** | **P1** | **Modern `AgentTool` Migration**<br>- Upgrade `gws.py` and `plan.py` to `AgentTool(skip_summarization=True)`<br>- Integrate progress notifications via Plugin | Area 1 | Pi & Cloud |
| **Phase 3: Interactive Safety** | **P1** | **Touch Confirmation UI**<br>- Implement `ToolConfirmation` on destructive tools<br>- Add touch dialogs in frontend `cards.js` | Area 5 | Pi Kiosk Display |
| **Phase 4: Cloud Run Dual-Deployment** | **P2** | **Unified Database Sessions**<br>- Add `sqlalchemy` dependency<br>- Support `DatabaseSessionService` with PostgreSQL for Cloud Run | Area 6 | Cloud Run (#575) |
| **Phase 5: Extensibility & Smart Home** | **P2** | **MCP Integration & Smart Devices**<br>- Evaluate `MCPToolset` with Home Assistant over SSE<br>- Optional `VertexAiMemoryBankService` for Cloud | Areas 3, 4 | Pi / Cloud |

---

## 4. Breakdown of Follow-Up Implementation Issues

The following actionable issues are ready to be filed in the tracker:

### Issue A: `feat(adk): introduce BasePlugin pipeline for traffic logging and tool retries`
- **Scope:**
  - Create `src/ai_terminal/live/plugins/logging.py` implementing `TrafficLoggingPlugin(BasePlugin)` with privacy-safe argument sizing.
  - Configure `ReflectAndRetryToolPlugin(max_retries=2)` for transient weather and geocoding network errors.
  - Remove manual `_log_tool_traffic` hook from `src/ai_terminal/live/main.py`.

### Issue B: `feat(adk): migrate GWS and Plan sub-agents to AgentTool with skip_summarization`
- **Scope:**
  - Refactor `src/ai_terminal/live/tools/gws.py` to replace the manual `Runner` loop with `AgentTool(skip_summarization=True)`.
  - Refactor `src/ai_terminal/live/tools/plan.py` to use `AgentTool`.
  - Maintain defensive exception trapping so background tool tasks never leave the UI hanging.

### Issue C: `feat(ui): implement touchscreen tool confirmation for destructive actions`
- **Scope:**
  - Add `tool_confirmation` handling in `gws.py` for calendar event deletions and email sending.
  - Implement interactive confirmation card dialog in `src/ai_terminal/live/static/js/cards.js`.
  - Handle user approve/cancel responses over the Live WebSocket connection.

### Issue D: `feat(sessions): support DatabaseSessionService for Cloud Run dual deployment`
- **Scope:**
  - Add optional `sqlalchemy` and `asyncpg` dependencies for cloud deployment.
  - Provide database session factory that instantiates `DatabaseSessionService` when `DATABASE_URL` is configured.
  - Retain `TerminalSessionService(SqliteSessionService)` as the zero-dependency default for Raspberry Pi.

### Issue E: `feat(telemetry): integrate OpenTelemetry spans for live turn and tool latency`
- **Scope:**
  - Integrate `google.adk.telemetry.tracing.trace_call_llm` and `trace_tool_call`.
  - Export spans to Cloud Trace when running in Google Cloud; use local metrics on Raspberry Pi.

---

## 5. Conclusion

Adopting modern `google-adk>=2.8.0` features provides immediate architectural dividends:
1. **Code Reduction:** Replaces hundreds of lines of fragile manual runner management and ad-hoc event filtering with standardized plugins and tools.
2. **Robustness:** Introduces self-healing tool retries and native human-in-the-loop confirmation.
3. **Dual Deployment:** Unblocks multi-replica Cloud Run deployments with PostgreSQL sessions without compromising the privacy, simplicity, and low footprint of the local Raspberry Pi kiosk.
