# F_81: Standalone Observability Rail

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-08-17 |
| 范围 | `openjiuwen/extensions/observability/rail.py`, `session_span.py`, `setup.py` |
| Refs | CORTEX roadmap (Agent-Core Observability Enhancement, Phase 2/3) |

## 背景

`F_37_observability-otel-trace.md` 描述的 span 树（`team.{name}` root →
`agent.{member}.task_iteration.{n}` → `llm.call` / `tool.{name}`）此前只对
Agent Team 内的成员可用。Team 的 root span 由
`agent_teams.observability.setup` / `Runner._maybe_attach_observability`
在 agent `invoke()` 之前创建；一个不在 Team 内运行的独立 `DeepAgent` /
`ControllerAgent` 没有对应的 host integration —— `callback_handler.py` 的
`on_agent_invoke_input` / `_output` 只向已存在的 root span 传播 query/output，
从不自己创建 root span（见其 docstring："Root span creation is owned by the
host integration"）。结果是独立 agent 的调用完全不产生任何 span：
`_get_parent_context_for_llm_tool()` 找不到有效 parent，静默跳过 span 创建。

## 本次改动

新增 `StandaloneObservabilityRail`（`extensions/observability/rail.py`），
为独立 agent 补上缺失的 host integration：

- **Session root span**：`before_invoke` 中懒创建一个按 `session_id` 键控的
  root span（`agent.{name}.session`，见 `session_span.py` 的
  `get_or_create_session_span` / `finalize_session_trace`），`after_invoke`
  中关闭。只有最外层的 `invoke()` 调用会创建/关闭它——`get_root_span()`
  已存在时（Team 场景，或嵌套的 standalone 调用）直接跳过，不会产生第二个
  root 或过早关闭仍在使用的 root。
- **Agent span**：复用 F_37 已验证的 `AgentSpanScope` 生命周期模型（本文件中
  为独立副本，不依赖 `agent_teams`），对多轮任务循环开
  `agent.{name}.task_iteration.{n}`，对单轮 agent 开 `agent.{name}.invoke`。
- 一旦 root span 存在，`llm.call` / `tool.{name}` 子 span 完全复用现有的
  `callback_handler.py` 逻辑——它已经会从 `get_current_agent_span()` 回退到
  `get_root_span()`，不需要任何改动。

## Span 树结构

```
agent.{name}.session                      ROOT（懒创建）
├── agent.{name}.task_iteration.1         AGENT
│     ├── llm.call                        GENERATION
│     └── tool.xxx                        TOOL
├── agent.{name}.task_iteration.2         AGENT
└── agent.{name}.invoke                   AGENT（单轮 agent）
    ├── llm.call
    └── tool.xxx
```

## 启用方式

独立 agent 需要：
1. 调用 `extensions.observability.setup.init_observability(config)` ——
   与 `agent_teams.observability.setup` 共享同一个 `ObservabilityRuntime`
   实例（见该文件顶部注释），因此 Team 和独立 agent 同进程内混用不会产生
   重复 span / callback 双重注册。
2. 在 DeepAgent spec 的 `rails` 列表中加入
   `harness.manifest.builtin_elements.STANDALONE_OBSERVABILITY`
   (`"core.observability.standalone"`)。这是一个独立于 Team 的
   `"core.observability"` 的 catalog 名字——两者绑定不同的 rail 类，
   同名会触发 catalog 的重复注册检查报错。

## 已知限制 / 后续工作

- `StandaloneObservabilityRail` 与 `agent_teams.observability.rail.ObservabilityRail`
  的 `AgentSpanScope` 逻辑重复（未合并为共享基类），以避免改动
  Team 侧已被大量测试覆盖的 rail。若未来需要消除重复，可考虑将两者收敛为
  一个基类 + 两个薄子类。
- 独立 agent 的 `task.{id}` 事件 span（Team 侧由 `monitor_handler.py` 产出）
  尚未提供；独立场景目前没有等价的任务事件流。
