"""LANGGraph VPN Diagnosis Agent — RuntimeView：向工具暴露服务端运行上下文。

模块归属：backend/vpn。设计要点：
    - 生产 `AgentRuntime`（backend/runtime.py）没有 `.context` 字段；而工具（tools.py
      与 copilot/tools.py 一致）从 `config["configurable"]["runtime"].context` 读取
      RunContext（租户/身份/scope）。若把完整 `AgentRuntime` 直接交给工具，会抛
      「工具缺少服务端运行上下文」→ 治理返回失败 → 无证据 → 全部转人工，功能形同虚设。
    - `RuntimeView` 把 RunContext 挂在 `.context` 上，并把 `vpn_adapter / metrics /
      tool_governance / audit / tickets / ticket_operations / assets / knowledge_retriever`
      等属性**委托代理**到底层 runtime。这使 Agent 经 `_runtime_config` 传给工具的 runtime
      能取到 `runtime.context.tenant_id`（租户隔离）与 `runtime.vpn_adapter`（Mock 数据源），
      与单元测试桩（SimpleNamespace(context=RunContext(...))）行为一致，且不污染共享 AgentRuntime。
    - 仅包装读取所需的属性；不引入任何写操作或副作用。
"""

from __future__ import annotations

from typing import Any


class RuntimeView:
    """把 RunContext 暴露为 `.context`，其余属性委托代理到底层 runtime。

    构造：RuntimeView(runtime, run_context)
        runtime   : 真实 AgentRuntime（提供 vpn_adapter / metrics / tool_governance / audit 等）
        run_context: 服务端 RunContext（tenant_id / user_id / scopes / allowed_tools），
                     挂到 `.context` 上供工具读取。

    委托方式：未在实例上定义的属性经 __getattr__ 转发到 runtime，故
    runtime.context / runtime.vpn_adapter / runtime.metrics / runtime.tool_governance
    等访问都安全。本对象不持有任何可写属性，纯只读包装。
    """

    __slots__ = ("_runtime", "context")

    def __init__(self, runtime: Any, run_context: Any) -> None:
        # object.__setattr__ 绕过 __slots__ 约束；context 作为真实实例属性存在，
        # 因此 __getattr__ 不会为它触发。
        object.__setattr__(self, "_runtime", runtime)
        object.__setattr__(self, "context", run_context)

    def __getattr__(self, name: str) -> Any:
        # 委托代理到底层 runtime（vpn_adapter / metrics / tool_governance / audit / ...）
        return getattr(object.__getattribute__(self, "_runtime"), name)

    def __repr__(self) -> str:
        runtime = object.__getattribute__(self, "_runtime")
        return f"RuntimeView(runtime={type(runtime).__name__}, context={bool(self.context)})"
