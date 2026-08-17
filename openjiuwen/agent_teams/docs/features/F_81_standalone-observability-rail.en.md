# F_81: Standalone Observability Rail

## Metadata

| Field | Value |
|---|---|
| Date | 2026-08-17 |
| Scope | `openjiuwen/extensions/observability/rail.py`, `session_span.py`, `setup.py` |
| Refs | CORTEX roadmap (Agent-Core Observability Enhancement, Phase 2/3) |

## Background

The span tree described in `F_37_observability-otel-trace.md` (`team.{name}` root → `agent.{member}.task_iteration.{n}` → `llm.call` / `tool.{name}`) was previously only available to members of an Agent Team. The Team's root span is created by `agent_teams.observability.setup` / `Runner._maybe_attach_observability` before the agent's `invoke()` runs. A standalone `DeepAgent` / `ControllerAgent` running outside a Team has no equivalent host integration — `callback_handler.py`'s `on_agent_invoke_input` / `_output` only propagate query/output onto an *already-existing* root span; they never create one themselves (see its docstring: "Root span creation is owned by the host integration"). The result was that standalone agent invocations produced **no spans at all**: `_get_parent_context_for_llm_tool()` found no valid parent and silently skipped span creation.

## This change

Added `StandaloneObservabilityRail` (`extensions/observability/rail.py`), which supplies the missing host integration for standalone agents:

- **Session root span**: `before_invoke` lazily creates a root span keyed by `session_id` (`agent.{name}.session`, via `session_span.py`'s `get_or_create_session_span` / `finalize_session_trace`), closed in `after_invoke`. Only the outermost `invoke()` call creates/closes it — when `get_root_span()` already resolves to something (a Team context, or a nested standalone call), it's skipped, so no duplicate root is created and nothing gets closed prematurely while still in use.
- **Agent span**: reuses the same `AgentSpanScope` lifecycle model already proven out in F_37 (a self-contained copy in this file, with no dependency on `agent_teams`) — opens `agent.{name}.task_iteration.{n}` for multi-round task-loop agents, or `agent.{name}.invoke` for single-round agents.
- Once the root span exists, `llm.call` / `tool.{name}` child spans fall out of the existing `callback_handler.py` logic unchanged — it already falls back from `get_current_agent_span()` to `get_root_span()`.

## Span tree shape

```
agent.{name}.session                      ROOT (created lazily)
├── agent.{name}.task_iteration.1         AGENT
│     ├── llm.call                        GENERATION
│     └── tool.xxx                        TOOL
├── agent.{name}.task_iteration.2         AGENT
└── agent.{name}.invoke                   AGENT (single-round agents)
    ├── llm.call
    └── tool.xxx
```

## How to enable it

A standalone agent needs to:
1. Call `extensions.observability.setup.init_observability(config)` — this shares the same `ObservabilityRuntime` instance as `agent_teams.observability.setup` (see the comment at the top of that file), so mixing Team and standalone agents in the same process doesn't cause duplicate spans or double callback registration.
2. Add `harness.manifest.builtin_elements.STANDALONE_OBSERVABILITY` (`"core.observability.standalone"`) to the DeepAgent spec's `rails` list. This is deliberately a different catalog name from Team's `"core.observability"` — they bind to different rail classes, and reusing the same name would trip the catalog's duplicate-registration check.

## Known limitations / follow-up

- `StandaloneObservabilityRail` duplicates the `AgentSpanScope` logic in `agent_teams.observability.rail.ObservabilityRail` rather than sharing a base class, to avoid touching the Team-side rail (which has heavy existing test coverage). If deduplication becomes worthwhile later, consider converging both into one base class with two thin subclasses.
- Standalone agents have no equivalent of the Team-side `task.{id}` event spans (produced by `monitor_handler.py`) — there's currently no equivalent task-event stream for the standalone case.
- Verified so far with `InMemorySpanExporter` (unit tests) and `ConsoleSpanExporter` (manual smoke test) only — not yet checked against a live OTLP backend (Langfuse / Grafana Tempo / Jaeger). The export path is unchanged from what Team traces already use, so this is expected to work, but hasn't been visually confirmed end-to-end.
