"""任务存储：内存索引 + JSONL 追加日志（可回放、可重启恢复）。

为什么不用数据库：单机演示场景下，追加式 JSONL 已经提供持久化与回放，
同时避免演示环境多一个依赖 —— DESIGN.md「选型判断」有此权衡。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from ..config import get_settings


class TaskStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (get_settings().db_abs_path.parent / "tasks.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()   # 守护管理面复合变更窗口（见 clear）
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                t = json.loads(line)
                self._tasks[t["id"]] = t
            except Exception:  # noqa: BLE001, S112  # 损坏行跳过（追加日志的有意容错）
                continue

    def create(self, question: str, role: str) -> dict:
        t = {
            "id": uuid.uuid4().hex[:12],
            "question": question,
            "role": role,
            "status": "running",
            "kind": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_ms": None,
            "answer": None,
            "usage": {},
            "events": [],
        }
        self._tasks[t["id"]] = t
        self._append(t)
        return t

    def finish(self, task_id: str, answer: dict, usage: dict, events: list, elapsed_ms: float) -> None:
        t = self._tasks[task_id]
        t.update(
            status="done",
            kind=(answer or {}).get("kind"),
            answer=answer,
            usage=usage,
            events=events,
            elapsed_ms=round(elapsed_ms, 1),
        )
        self._append(t)

    def set_feedback(self, task_id: str, value: str, comment: str = "") -> dict | None:
        """thumbs 反馈：附着到任务并随任务一起追加落盘（重启可回放）。"""
        t = self._tasks.get(task_id)
        if t is None:
            return None
        t["feedback"] = {"value": value, "comment": comment,
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._append(t)
        return t

    def _append(self, t: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")

    def clear(self, audit: Callable[[dict], None] | None = None) -> dict:
        """管理面任务清除：JSONL 归档改名 + 内存索引清空。

        审计与变更在同一把锁窗口内先后落盘（"同事务"的 JSONL 翻译，语义见 app/audit.py
        模块头）：审计回调写失败 → 立即回滚（归档文件改名回去 + 内存索引恢复）并向上抛，
        不存在"改了但没记"或"记了但没改"的窗口。
        """
        with self._lock:
            snapshot = dict(self._tasks)
            removed = len(snapshot)
            archived_to = self._rotate_file()
            self._tasks.clear()
            info = {"removed": removed, "archived_to": archived_to}
            if audit is not None:
                try:
                    audit(info)
                except Exception:
                    self._restore_file(archived_to)
                    self._tasks.update(snapshot)
                    raise
            return info

    def _rotate_file(self) -> str:
        """tasks.jsonl → tasks.jsonl.bak-<时间戳>-<随机>（归档可追溯，改名是原子操作）。"""
        if not self.path.exists():
            return ""
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}"
        dest = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        self.path.rename(dest)
        return dest.name

    def _restore_file(self, archived_to: str) -> None:
        if archived_to:
            dest = self.path.with_name(archived_to)
            if dest.exists():
                dest.rename(self.path)

    def list(self, limit: int = 50) -> list[dict]:
        items = sorted(self._tasks.values(), key=lambda x: x["created_at"], reverse=True)
        return [{k: v for k, v in t.items() if k not in ("answer", "events", "usage")} for t in items[:limit]]

    def get(self, task_id: str) -> dict | None:
        return self._tasks.get(task_id)

    def usage_stats(self) -> dict:
        total_prompt = total_completion = 0
        by_node: dict[str, dict] = {}
        feedback: dict[str, int] = {"up": 0, "down": 0}
        for t in self._tasks.values():
            fb = (t.get("feedback") or {}).get("value")
            if fb in feedback:
                feedback[fb] += 1
            for node, u in (t.get("usage") or {}).items():
                total_prompt += u.get("prompt_tokens", 0)
                total_completion += u.get("completion_tokens", 0)
                cur = by_node.setdefault(node, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
                cur["prompt_tokens"] += u.get("prompt_tokens", 0)
                cur["completion_tokens"] += u.get("completion_tokens", 0)
                cur["calls"] += 1
        return {
            "tasks": len(self._tasks),
            "total_tokens": total_prompt + total_completion,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "by_node": by_node,
            "feedback": feedback,
        }


_STORE: TaskStore | None = None


def get_task_store() -> TaskStore:
    global _STORE
    if _STORE is None:
        _STORE = TaskStore()
    return _STORE
