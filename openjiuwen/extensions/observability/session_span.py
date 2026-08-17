# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Root-span host integration for standalone (non-Team) agent invocations.

Agent Teams get a root span from ``agent_teams.observability.span_context``
(``get_or_create_team_span`` / ``finalize_trace``), keyed by team name. A
standalone ``DeepAgent`` / ``ControllerAgent`` invocation has no team, so it
never got a root span at all — its LLM/tool spans had no parent to attach to
and ``extensions.observability.callback_handler`` silently skipped creating
them (see ``_get_parent_context_for_llm_tool``).

This module is the same pattern applied to the generic, session-keyed case:
one root span per ``session_id``, created before the standalone invocation
and closed in the caller's ``finally``. It builds entirely on the already
session-keyed primitives in ``span_context`` (``set_root_span`` /
``get_root_span`` / ``clear_root_span`` accept ``session_id`` themselves) —
no new span state is introduced here.
"""

from __future__ import annotations

from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

from openjiuwen.core.common.logging import logger
from openjiuwen.extensions.observability.semconv import (
    AT_AGENT_ID,
    AT_AGENT_NAME,
    AT_SESSION_ID,
    LANGFUSE_SESSION_ID,
    LANGFUSE_TRACE_NAME,
    LANGFUSE_TRACE_TAGS,
)
from openjiuwen.extensions.observability.span_context import (
    clear_root_span,
    flush_child_spans,
    get_bound_root_span,
    get_root_span,
    set_root_span,
)


def get_or_create_session_span(session_id: str, agent_name: str, tracer: Tracer) -> Span | None:
    """Return the live root span for *session_id*, creating one if needed.

    Mirrors ``agent_teams.observability.span_context.get_or_create_team_span``
    for the generic, non-Team case. A missing ``session_id`` means there is
    no key to register a root under — callers skip observability for that
    invocation rather than creating an unregistered, unfindable span.
    """
    if not session_id:
        return None
    existing = get_bound_root_span()
    if existing is not None:
        return existing

    span = tracer.start_span(name=f"agent.{agent_name}.session", kind=SpanKind.SERVER)
    span.set_attribute(AT_SESSION_ID, session_id)
    span.set_attribute(LANGFUSE_SESSION_ID, session_id)
    if agent_name:
        span.set_attribute(AT_AGENT_ID, agent_name)
        span.set_attribute(AT_AGENT_NAME, agent_name)
    span.set_attribute(LANGFUSE_TRACE_NAME, f"agent.{agent_name}.session" if agent_name else "agent.session")
    span.set_attribute(LANGFUSE_TRACE_TAGS, [agent_name] if agent_name else [])
    set_root_span(span, session_id=session_id)
    logger.debug(
        "otel: get_or_create_session_span CREATE session_id={} agent_name={} "
        "trace_id={:032x} span_id={:016x}",
        session_id, agent_name, span.context.trace_id, span.context.span_id,
    )
    return span


def finalize_session_trace(session_id: str) -> None:
    """Close the session root span and flush only its trace's child spans."""
    span = get_root_span(session_id=session_id)
    trace_id = getattr(getattr(span, "context", None), "trace_id", None)
    if span is not None and span.is_recording():
        span.set_status(Status(StatusCode.OK))
        span.end()
    if span is not None:
        clear_root_span(session_id=session_id, expected_span=span)
    flush_child_spans(trace_id=trace_id)


__all__ = [
    "finalize_session_trace",
    "get_or_create_session_span",
]
