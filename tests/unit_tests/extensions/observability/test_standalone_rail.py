# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Span-tree tests for ``StandaloneObservabilityRail``.

Mirrors ``tests/unit_tests/agent_teams/observability/test_observability.py``'s
style (drive the rail directly + real callback framework, assert on spans
collected by ``InMemorySpanExporter``) but for a DeepAgent that has no Team —
before this rail existed, such an agent produced zero spans at all (see
``extensions/observability/rail.py`` module docstring for why).
"""

from __future__ import annotations

from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import (
    AgentEvents,
    LLMCallEvents,
    ToolCallEvents,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    TaskIterationInputs,
)
from openjiuwen.extensions.observability.config import ObservabilityConfig
from openjiuwen.extensions.observability.rail import StandaloneObservabilityRail
from openjiuwen.extensions.observability.semconv import AT_SESSION_ID
from openjiuwen.extensions.observability.setup import init_observability, shutdown_observability
from openjiuwen.extensions.observability.span_context import get_root_span, reset_state


@pytest.fixture
def in_memory_exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    init_observability(
        ObservabilityConfig(enabled=True, service_name="openjiuwen-test", sample_rate=1.0),
        span_exporter_override=exporter,
    )
    yield exporter
    shutdown_observability()
    reset_state()


def _spans_by_name(exporter: InMemorySpanExporter, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def _attr(span: Any, key: str, default: Any = None) -> Any:
    return dict(span.attributes or {}).get(key, default)


def _mock_agent(*, enable_task_loop: bool, name: str = "solo") -> Any:
    agent = MagicMock()
    card = MagicMock()
    card.name = name
    agent.card = card
    deep_config = MagicMock()
    deep_config.enable_task_loop = enable_task_loop
    agent.deep_config = deep_config
    return agent


def _mock_session(session_id: str) -> Any:
    session = MagicMock()
    session.get_session_id.return_value = session_id
    return session


@pytest.mark.asyncio
async def test_no_root_span_before_this_rail_runs(in_memory_exporter: InMemorySpanExporter) -> None:
    """A standalone AGENT_INVOKE_INPUT without the rail creates no spans.

    Documents the gap this rail closes: the generic callback handler alone
    (no host integration) never creates a root span, so LLM spans have no
    parent and are silently skipped.
    """
    fw = Runner.callback_framework
    session = _mock_session("solo-session-baseline")
    await fw.trigger(AgentEvents.AGENT_INVOKE_INPUT, {"user_input": "hi"}, session=session)
    await fw.trigger(LLMCallEvents.LLM_INVOKE_INPUT, messages=[{"role": "user", "content": "hi"}], model="fake-llm")
    await fw.trigger(LLMCallEvents.LLM_INVOKE_OUTPUT, messages=[], result="hi back")

    assert get_root_span(session_id="solo-session-baseline") is None
    assert _spans_by_name(in_memory_exporter, "llm.call") == []


@pytest.mark.asyncio
async def test_single_round_invoke_produces_session_and_agent_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """before_invoke/after_invoke alone (single-round agent) yields session -> agent.invoke -> llm/tool."""
    rail = StandaloneObservabilityRail()
    agent = _mock_agent(enable_task_loop=False, name="solo")
    session = _mock_session("solo-session-1")
    inputs = InvokeInputs(query="do the thing")
    ctx = AgentCallbackContext(agent=agent, inputs=inputs, session=session)

    await rail.before_invoke(ctx)

    root_span = get_root_span(session_id="solo-session-1")
    assert root_span is not None
    assert root_span.name == "agent.solo.session"

    fw = Runner.callback_framework
    messages = [{"role": "user", "content": "do the thing"}]
    await fw.trigger(LLMCallEvents.LLM_INVOKE_INPUT, messages=messages, model="fake-llm-1")
    await fw.trigger(
        ToolCallEvents.TOOL_CALL_STARTED, tool_name="calc", tool_id="calc-1", inputs=((), {"expr": "6*7"})
    )
    await fw.trigger(
        ToolCallEvents.TOOL_CALL_FINISHED,
        tool_name="calc",
        tool_id="calc-1",
        inputs=((), {"expr": "6*7"}),
        result=42,
    )
    await fw.trigger(LLMCallEvents.LLM_INVOKE_OUTPUT, messages=messages, result="42")

    inputs.result = "42"
    await rail.after_invoke(ctx)

    agent_spans = _spans_by_name(in_memory_exporter, "agent.solo.invoke")
    llm_spans = _spans_by_name(in_memory_exporter, "llm.call")
    tool_spans = _spans_by_name(in_memory_exporter, "tool.calc")
    root_spans = [s for s in in_memory_exporter.get_finished_spans() if s.name == "agent.solo.session"]

    assert len(agent_spans) == 1
    assert len(llm_spans) == 1
    assert len(tool_spans) == 1
    assert len(root_spans) == 1, "session root must close in after_invoke"

    assert agent_spans[0].parent.span_id == root_spans[0].context.span_id
    assert llm_spans[0].parent.span_id == agent_spans[0].context.span_id
    assert tool_spans[0].parent.span_id == agent_spans[0].context.span_id
    assert _attr(root_spans[0], AT_SESSION_ID) == "solo-session-1"

    # Root span closing must be the last thing exported for this trace.
    assert get_root_span(session_id="solo-session-1") is None


@pytest.mark.asyncio
async def test_multi_round_task_loop_nests_iterations_under_one_session(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """before_invoke opens the session root once; two iterations nest under it, not under each other."""
    rail = StandaloneObservabilityRail()
    agent = _mock_agent(enable_task_loop=True, name="looper")
    session = _mock_session("solo-session-2")
    invoke_inputs = InvokeInputs(query="multi step task")
    ctx = AgentCallbackContext(agent=agent, inputs=invoke_inputs, session=session)

    await rail.before_invoke(ctx)
    root_span = get_root_span(session_id="solo-session-2")
    assert root_span is not None

    for i in (1, 2):
        iter_ctx = AgentCallbackContext(
            agent=agent,
            inputs=TaskIterationInputs(iteration=i, query=f"step {i}", loop_event=None),
            session=session,
        )
        await rail.before_task_iteration(iter_ctx)
        await rail.after_task_iteration(iter_ctx)

    invoke_inputs.result = "done"
    await rail.after_invoke(ctx)

    iteration_spans = [
        s for s in in_memory_exporter.get_finished_spans() if s.name.startswith("agent.looper.task_iteration")
    ]
    root_spans = [s for s in in_memory_exporter.get_finished_spans() if s.name == "agent.looper.session"]

    assert {s.name for s in iteration_spans} == {
        "agent.looper.task_iteration.1",
        "agent.looper.task_iteration.2",
    }
    assert len(root_spans) == 1
    for span in iteration_spans:
        assert span.parent.span_id == root_spans[0].context.span_id, "iterations must nest under the session, not siblings-of-siblings"

    assert get_root_span(session_id="solo-session-2") is None
