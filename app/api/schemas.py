"""API 请求/响应模型（pydantic v2）：边界即契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    role: Literal["boss", "south", "north"] = "boss"
    session_id: str = ""   # 多轮指代消解用；留空视为独立单轮
    thread_id: str = ""    # E4② 澄清挂起：同 thread 恢复；留空自动用 task id
    resume: bool = False   # True 时 question 视为对挂起澄清的回答（Command(resume)）
    # 留空则由预算路由自动决定；显式传入可覆盖（评测用）
    depth: Literal["", "light", "full"] = ""


class FeedbackRequest(BaseModel):
    task_id: str = Field(min_length=1)
    value: Literal["up", "down"]
    comment: str = Field(default="", max_length=200)


class AdminConfigRequest(BaseModel):
    """管理面运行期配置变更（from→to 全程入审计日志；只开放无副作用的演示开关）。

    未提供的字段不动；显式 null 视为未提供（不产生"改回默认值"的意外变更）。
    """

    cache_enabled: bool | None = None
    followup_enabled: bool | None = None
    clarify_suspend_enabled: bool | None = None


class TaskSummary(BaseModel):
    id: str
    question: str
    role: str
    status: str
    kind: str | None = None
    created_at: str
    elapsed_ms: float | None = None


class TaskDetail(TaskSummary):
    answer: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    events: list[dict] | None = None
