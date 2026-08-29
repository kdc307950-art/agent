"""工作流 spec 加载层。

当前实现：从 `AGENT_WORKFLOW_PATH` 指向的 JSON 文件读取，启动时加载一次。

**这里刻意只暴露一个函数入口**。后续要换成「读 Postgres 表 + 按租户取激活版本 +
热编译缓存」时，只需替换 `load_workflow_spec` 的实现，`runtime.py` 的调用方不用动。
相关取舍见桌面文档《langgraph-编排层技术决策记录.md》。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("langgraph.workflow.loader")


class WorkflowSpecUnavailable(RuntimeError):
    """工作流定义缺失或不可读，应当在启动阶段直接失败（fail fast）。"""


def load_workflow_spec(settings: Any) -> dict[str, Any]:
    """按配置读取工作流 spec，返回未经校验的原始 dict。

    结构校验交给 `build_workflow_from_json` 内的 pydantic 模型，这样错误信息
    统一由编译层给出，加载层只负责「能不能拿到内容」。
    """
    raw_path = getattr(settings, "agent_workflow_path", None)
    if not raw_path:
        raise WorkflowSpecUnavailable("AGENT_GRAPH_MODE=workflow 但未配置 AGENT_WORKFLOW_PATH")
    path = Path(raw_path)
    if not path.is_absolute():
        # 相对路径按项目根目录解析，避免受进程工作目录影响
        path = Path(__file__).resolve().parents[1] / path
    if not path.is_file():
        raise WorkflowSpecUnavailable(f"工作流定义文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            spec = json.load(fh)
    except json.JSONDecodeError as exc:
        raise WorkflowSpecUnavailable(f"工作流定义不是合法 JSON: {path} ({exc})") from exc
    except OSError as exc:
        raise WorkflowSpecUnavailable(f"工作流定义读取失败: {path} ({exc})") from exc
    if not isinstance(spec, dict):
        raise WorkflowSpecUnavailable(f"工作流定义必须是 JSON 对象: {path}")
    logger.info("已加载工作流定义 name=%s path=%s", spec.get("name", "?"), path)
    return spec
