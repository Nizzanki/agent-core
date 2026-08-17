# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observability rail primitives for standalone (non-Team) agents.

``AgentSpanScope`` here is a self-contained copy of the span-lifecycle
helper of the same name in ``agent_teams.observability.rail`` — it never
depended on Team concepts, but is duplicated rather than imported so this
module has no dependency on ``agent_teams`` and touching the well-tested
Team rail is never required to change standalone behavior (or vice versa).

``StandaloneObservabilityRail`` is a new DeepAgent rail for agents running
outside a Team. Team agents get their root span from
``agent_teams.observability.setup.init_observability`` /
``attach_to_team_agent`` before the agent's ``invoke()`` ever runs; a
standalone agent has no such host, so this rail creates one lazily — a
session-keyed root span (see ``session_span.py``) opened on the outermost
``before_invoke`` and closed on the matching ``after_invoke``. Once that root
exists, the same ``agent.{name}.task_iteration.{n}`` / ``agent.{name}.invoke``
span shape Team already produces (see
``agent_teams/docs/features/F_37_observability-otel-trace.md``) falls out of
the existing generic LLM/tool span handling in ``callback_handler.py``
without further changes — it already resolves its parent via
``get_current_agent_span()`` falling back to ``get_root_span()``.
"""

from __future__ import annotations

from importlib.util import find_spec
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    set_span_in_context,
)

from openjiuwen.core.common.logging import logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.extensions.observability.redaction import redact_completion, redact_prompt
from openjiuwen.extensions.observability.semconv import (
    AT_AGENT_ID,
    AT_AGENT_INPUT,
    AT_AGENT_NAME,
    AT_AGENT_OUTPUT,
    AT_AGENT_ROLE,
    AT_MEMBER_ID,
    AT_MEMBER_NAME,
    AT_SESSION_ID,
    DA_TASK_IS_FOLLOW_UP,
    DA_TASK_ITERATION,
    DA_TASK_LOOP_EVENT,
    LANGFUSE_OBSERVATION_INPUT,
    LANGFUSE_OBSERVATION_OUTPUT,
    LANGFUSE_OBSERVATION_TYPE,
    LANGFUSE_SESSION_ID,
)
from openjiuwen.extensions.observability.session_span import (
    finalize_session_trace,
    get_or_create_session_span,
)
from openjiuwen.extensions.observability.span_context import (
    cascade_close_children,
    clear_tool_span_context,
    get_current_agent_span,
    get_current_tool_span,
    get_root_span,
    set_current_agent_span,
)
from openjiuwen.harness.rails.base import DeepAgentRail

_TRACER_NAME = "openjiuwen.extensions.observability.rail"


class AgentSpanScope:
    """Owns the lifecycle of one open agent span and its nesting decision.

    Nesting is decided structurally: the current agent span (from the
    ``_current_agent_span`` ContextVar) is the legitimate parent whenever
    it is still recording. The scope remembers the parent it nested under
    so ``close`` can restore it as current when the child returns.

    The scope does NOT touch the inherited llm/tool stacks on the nested
    path: those belong to the still-open parent and are closed by the
    parent's own scope. Cascade-close runs only when this scope is the
    outermost agent (iteration path).

    The scope is parked on ``ctx.extra`` for the duration of one span —
    opened in ``before_task_iteration`` / ``before_invoke`` and retrieved
    by the matching ``after_*``. ``ctx.extra`` is per-callback-context, so
    it does not leak across asyncio tasks the way a ContextVar would under
    iteration/invoke nesting.
    """

    KIND_ITERATION = "iteration"
    KIND_INVOKE = "invoke"

    _CTX_KEY = "_otel_agent_scope"

    def __init__(
        self,
        *,
        span: Span,
        kind: str,
        parent_agent_span: Span | None,
        is_outermost: bool,
        config: Any,
    ) -> None:
        self.span = span
        self.kind = kind
        # The agent span that was current when this scope opened — restored
        # as _current_agent_span on close. None when nested directly under
        # the root span (no agent-tier parent).
        self.parent_agent_span = parent_agent_span
        # True when this scope owns the cascade-close of child llm/tool
        # spans (iteration path, or an invoke scope with no agent parent).
        self.is_outermost = is_outermost
        # ObservabilityConfig captured at open time; None disables redaction
        # and the close path stores the raw output string.
        self._config = config

    @classmethod
    def current(cls, ctx: AgentCallbackContext) -> AgentSpanScope | None:
        """Return the scope parked on this callback context, or None."""
        return ctx.extra.get(cls._CTX_KEY)

    def attach(self, ctx: AgentCallbackContext) -> None:
        """Park this scope on the callback context for the matching after_*."""
        ctx.extra[self._CTX_KEY] = self

    @classmethod
    def detach(cls, ctx: AgentCallbackContext) -> AgentSpanScope | None:
        """Pop and return the scope parked on this callback context."""
        return ctx.extra.pop(cls._CTX_KEY, None)

    def close(self, *, output: Any, exception: BaseException | None) -> None:
        """End this scope's span and restore the parent as current."""
        span = self.span
        if not span.is_recording():
            return
        if output:
            output_str = str(output)
            redacted = redact_completion(output_str, self._config) if self._config else output_str
            span.set_attribute(LANGFUSE_OBSERVATION_OUTPUT, redacted)
            span.set_attribute(AT_AGENT_OUTPUT, redacted)

        if self.is_outermost:
            cascade_close_children()

        if exception is not None:
            span.record_exception(exception)
            span.set_status(Status(StatusCode.ERROR, str(exception)))
        else:
            span.set_status(Status(StatusCode.OK))
        span.end()

        # Restore the parent agent span (None when there was none) so the
        # parent's subsequent llm/tool spans resume nesting correctly.
        set_current_agent_span(self.parent_agent_span)
        if self.parent_agent_span is not None and self.parent_agent_span.is_recording():
            parent_ctx = set_span_in_context(self.parent_agent_span, otel_context.get_current())
            otel_context.attach(parent_ctx)


class StandaloneObservabilityRail(DeepAgentRail):
    """Create AGENT spans for a DeepAgent running outside a Team.

    Span tree (once a session root exists):
      agent.{name}.session                     ROOT (created lazily here)
      ├── agent.{name}.task_iteration.1         [AGENT]
      │     ├── llm.call                        [GENERATION]
      │     └── tool.xxx                        [TOOL]
      ├── agent.{name}.task_iteration.2          [AGENT]
      └── agent.{name}.invoke                    [AGENT] (single-round agents)
    """

    priority: int = 10

    _SESSION_ROOT_CTX_KEY = "_otel_session_root_owned"

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        super().__init__()
        self._injected_tracer = tracer
        # See ObservabilityRail's identical field for why this needs no
        # per-context storage: one rail instance belongs to one agent, and a
        # DeepAgent runs one invoke at a time.
        self._open_invoke_span: Span | None = None

    def _tracer(self) -> Tracer:
        if self._injected_tracer is not None:
            return self._injected_tracer
        from openjiuwen.extensions.observability.setup import get_tracer
        return get_tracer(_TRACER_NAME)

    @staticmethod
    def _resolve_session_id(ctx: AgentCallbackContext) -> str:
        session = getattr(ctx, "session", None)
        if session is None:
            return ""
        try:
            return session.get_session_id() or ""
        except Exception as exc:
            logger.warning("standalone otel rail: failed to get session_id: {}", exc)
            return ""

    @staticmethod
    def _resolve_agent_name(agent: Any) -> str:
        card = getattr(agent, "card", None)
        name = getattr(card, "name", None)
        return name if isinstance(name, str) and name else "agent"

    def _ensure_session_root(self, ctx: AgentCallbackContext) -> None:
        """Lazily open a session root span for the outermost standalone invoke.

        A no-op whenever a root span is already bound — a Team member
        already has one from ``attach_to_team_agent``, and a nested
        standalone invoke (subagent dispatch) inherits the outer invoke's
        root through the same ContextVar. Only the true outermost standalone
        call creates one, and only that call's ``after_invoke`` closes it.
        """
        if get_root_span() is not None:
            return
        session_id = self._resolve_session_id(ctx)
        if not session_id:
            return
        agent_name = self._resolve_agent_name(ctx.agent)
        span = get_or_create_session_span(session_id, agent_name, self._tracer())
        if span is not None:
            ctx.extra[self._SESSION_ROOT_CTX_KEY] = session_id

    def _maybe_close_session_root(self, ctx: AgentCallbackContext) -> None:
        session_id = ctx.extra.pop(self._SESSION_ROOT_CTX_KEY, None)
        if session_id:
            finalize_session_trace(session_id)

    async def before_task_iteration(self, ctx: AgentCallbackContext) -> None:
        try:
            inputs = ctx.inputs
            iteration = int(getattr(inputs, "iteration", 0) or 0)
            is_follow_up = bool(getattr(inputs, "is_follow_up", False))

            agent = ctx.agent
            agent_name = self._resolve_agent_name(agent)

            root_span = get_root_span()
            if root_span is None or not root_span.is_recording():
                return

            session_id = self._resolve_session_id(ctx)

            if AgentSpanScope.current(ctx) is not None:
                return

            self._drain_or_clear_stale(agent_name)

            iteration_parent = root_span
            if self._open_invoke_span is not None and self._open_invoke_span.is_recording():
                iteration_parent = self._open_invoke_span
            parent_ctx = set_span_in_context(iteration_parent, otel_context.get_current())
            span = self._tracer().start_span(
                name=f"agent.{agent_name}.task_iteration.{iteration}",
                context=parent_ctx,
                kind=SpanKind.INTERNAL,
            )

            self._stamp_agent_attributes(span, agent_name=agent_name, session_id=session_id)
            span.set_attribute(DA_TASK_ITERATION, iteration)
            span.set_attribute(DA_TASK_IS_FOLLOW_UP, is_follow_up)

            from openjiuwen.extensions.observability.setup import get_config
            config = get_config()
            query = getattr(inputs, "query", "") or ""
            if query:
                redacted_query = redact_prompt(query, config) if config else str(query)
                span.set_attribute(LANGFUSE_OBSERVATION_INPUT, redacted_query)
                span.set_attribute(AT_AGENT_INPUT, redacted_query)
            loop_event = getattr(inputs, "loop_event", None)
            if loop_event is not None:
                span.set_attribute(DA_TASK_LOOP_EVENT, str(loop_event))

            set_current_agent_span(span)
            agent_ctx = set_span_in_context(span, otel_context.get_current())
            otel_context.attach(agent_ctx)

            AgentSpanScope(
                span=span,
                kind=AgentSpanScope.KIND_ITERATION,
                parent_agent_span=None,
                is_outermost=True,
                config=config,
            ).attach(ctx)
        except Exception as exc:
            logger.warning("standalone otel rail before_task_iteration failed: {}", exc)

    async def after_task_iteration(self, ctx: AgentCallbackContext) -> None:
        try:
            scope: AgentSpanScope | None = AgentSpanScope.detach(ctx)
            if scope is None:
                return

            output = None
            inputs = getattr(ctx, "inputs", None)
            if inputs is not None:
                output = getattr(inputs, "result", None)

            scope.close(output=output, exception=ctx.exception)

            root_span = get_root_span()
            if root_span is not None and root_span.is_recording():
                root_ctx = set_span_in_context(root_span, otel_context.get_current())
                otel_context.attach(root_ctx)
        except Exception as exc:
            logger.warning("standalone otel rail after_task_iteration failed: {}", exc)

    # ------------------------------------------------------------------
    # Invoke-level: opens the session root (always) and, for single-round
    # agents only, an agent span (mirrors ObservabilityRail's before_invoke).
    # ------------------------------------------------------------------

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            self._ensure_session_root(ctx)

            if AgentSpanScope.current(ctx) is not None:
                return

            agent = ctx.agent
            deep_config = getattr(agent, "deep_config", None)
            enable_task_loop = bool(getattr(deep_config, "enable_task_loop", False))
            if enable_task_loop:
                return

            inputs = ctx.inputs
            agent_name = self._resolve_agent_name(agent)

            root_span = get_root_span()
            if root_span is None or not root_span.is_recording():
                return

            session_id = self._resolve_session_id(ctx)

            prev = get_current_agent_span()
            parent_span: Span
            is_outermost: bool
            if prev is not None and prev.is_recording():
                parent_span = prev
                is_outermost = False
            else:
                parent_span = root_span
                is_outermost = True
                if prev is not None:
                    set_current_agent_span(None)

            dispatch_tool_span = get_current_tool_span()
            otel_parent: Span = parent_span
            if (
                dispatch_tool_span is not None
                and dispatch_tool_span.parent is not None
                and dispatch_tool_span.parent.span_id == parent_span.context.span_id
            ):
                otel_parent = dispatch_tool_span

            parent_ctx = set_span_in_context(otel_parent, otel_context.get_current())
            span = self._tracer().start_span(
                name=f"agent.{agent_name}.invoke",
                context=parent_ctx,
                kind=SpanKind.INTERNAL,
            )
            self._stamp_agent_attributes(span, agent_name=agent_name, session_id=session_id)

            from openjiuwen.extensions.observability.setup import get_config
            config = get_config()
            query = getattr(inputs, "query", "") or ""
            if query:
                redacted_query = redact_prompt(query, config) if config else str(query)
                span.set_attribute(LANGFUSE_OBSERVATION_INPUT, redacted_query)
                span.set_attribute(AT_AGENT_INPUT, redacted_query)

            set_current_agent_span(span)
            agent_ctx = set_span_in_context(span, otel_context.get_current())
            otel_context.attach(agent_ctx)

            parent_agent_span = prev if parent_span is prev else None
            AgentSpanScope(
                span=span,
                kind=AgentSpanScope.KIND_INVOKE,
                parent_agent_span=parent_agent_span,
                is_outermost=is_outermost,
                config=config,
            ).attach(ctx)
            self._open_invoke_span = span
        except Exception as exc:
            logger.warning("standalone otel rail before_invoke failed: {}", exc)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            scope: AgentSpanScope | None = AgentSpanScope.detach(ctx)
            if scope is not None and scope.kind == AgentSpanScope.KIND_INVOKE:
                if scope.span is self._open_invoke_span:
                    self._open_invoke_span = None

                output = None
                inputs = getattr(ctx, "inputs", None)
                if inputs is not None:
                    output = getattr(inputs, "result", None)

                scope.close(output=output, exception=ctx.exception)
        except Exception as exc:
            logger.warning("standalone otel rail after_invoke failed: {}", exc)
        finally:
            try:
                self._maybe_close_session_root(ctx)
            except Exception as exc:
                logger.warning("standalone otel rail: failed to close session root: {}", exc)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _drain_or_clear_stale(self, agent_name: str) -> None:
        prev = get_current_agent_span()
        if prev is None or not prev.is_recording():
            return
        if prev is self._open_invoke_span:
            return
        prev_member = prev.attributes.get(AT_MEMBER_NAME, "")
        if prev_member == agent_name:
            logger.warning("standalone otel rail: closing orphan agent span: {}", getattr(prev, "name", "unknown"))
            cascade_close_children()
            prev.end()
        else:
            clear_tool_span_context()
        set_current_agent_span(None)

    @staticmethod
    def _stamp_agent_attributes(span: Span, *, agent_name: str, session_id: str) -> None:
        span.set_attribute(LANGFUSE_OBSERVATION_TYPE, "agent")
        span.set_attribute(AT_AGENT_ID, agent_name)
        span.set_attribute(AT_AGENT_NAME, agent_name)
        span.set_attribute(AT_MEMBER_ID, agent_name)
        span.set_attribute(AT_MEMBER_NAME, agent_name)
        span.set_attribute(AT_AGENT_ROLE, agent_name)
        if session_id:
            span.set_attribute(AT_SESSION_ID, session_id)
            span.set_attribute(LANGFUSE_SESSION_ID, session_id)


def maybe_standalone_observability_rail() -> StandaloneObservabilityRail | None:
    """Return a ``StandaloneObservabilityRail`` when observability is on, else None.

    Mirrors ``agent_teams.observability.rail.maybe_observability_rail`` for
    the standalone-agent runtime (``extensions.observability.setup``, a
    separate ``ObservabilityRuntime`` instance from the Team one).
    """
    from openjiuwen.extensions.observability.setup import is_initialized

    if not is_initialized():
        return None
    return StandaloneObservabilityRail()


def observability_dependency_installed() -> bool:
    """Report whether the optional ``observability`` extra is importable.

    Same probe as ``agent_teams.rails.elements.observability_dependency_installed``
    (duplicated rather than imported to avoid a harness -> agent_teams
    dependency): every observability module imports ``opentelemetry`` at
    module scope, so the dependency must be probed before the package is
    touched.
    """
    try:
        return find_spec("opentelemetry.sdk") is not None
    except (ImportError, ValueError):
        return False


__all__ = [
    "AgentSpanScope",
    "StandaloneObservabilityRail",
    "maybe_standalone_observability_rail",
    "observability_dependency_installed",
]
