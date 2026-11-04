"""管理面审计日志：谁在何时通过什么对什么做了什么（只追加 JSONL，结构化字段）。

对齐 agentgate 的 admin 审计模式（app/api/admin.py::_audit + AuditLog 表）：

- **结构化字段**：actor / action / target_type / target_id / detail——detail 只放
  非敏感摘要（计数、归档文件名、配置 from→to）；密钥类内容永不入审计面
  （本仓库密钥都在 .env，配置端点只收布尔开关，天然隔离）。
- **"同事务"语义的 JSONL 翻译**：agentgate 用 SQLAlchemy 同 session 提交实现
  "审计与业务变更要么都落要么都不落"；本仓库存储层是追加 JSONL（无 DB，见
  task_store 模块头的权衡），对应实现 = 变更与审计在**同一把锁窗口**内先后落盘，
  审计写失败 → 已执行变更立即回滚（任务清除=归档改名回去、缓存清空=条目恢复、
  配置变更=写回原值）并向客户端返回 500——"改了但没记"/"记了但没改"两个窗口都关死。
- **只追加**：审计行一经写入不修改不删除；查询端点（GET /api/admin/audit）只读。

多 worker 注记：JSONL 追加在多进程下行级原子性靠单行小写入；任务清除的归档
改名假定单 worker 口径（演示/单机部署）——多 worker 管理面属 PRODUCTION_GAP。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import get_settings


class AuditLog:
    """追加式 JSONL 审计日志（损坏行跳过，容错策略与 TaskStore 一致）。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (get_settings().db_abs_path.parent / "audit.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()   # 追加原子性：并发管理操作的行不交叉

    def record(
        self, actor: str, action: str, target_type: str, target_id: str,
        detail: dict | None = None,
    ) -> dict:
        event = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "actor": actor,
            "action": action,             # task.clear / cache.clear / config.update / ...
            "target_type": target_type,   # task_store / query_cache / settings / ...
            "target_id": target_id,
            "detail": detail or {},
        }
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return event

    def list(self, limit: int = 100) -> list[dict]:
        """最新在前（与 /api/tasks 的阅读顺序一致）。"""
        if not self.path.exists():
            return []
        events: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except Exception:  # noqa: BLE001, S112  # 损坏行跳过（追加日志的有意容错，与 TaskStore 同策略）
                continue
        return list(reversed(events))[:limit]


_AUDIT: AuditLog | None = None


def get_audit_log() -> AuditLog:
    global _AUDIT
    if _AUDIT is None:
        _AUDIT = AuditLog()
    return _AUDIT
