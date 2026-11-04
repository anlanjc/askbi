"""任务存储：内存索引 + JSONL 追加日志（可回放、可重启恢复）。

为什么不用数据库：单机演示场景下，追加式 JSONL 已经提供持久化与回放，
同时避免演示环境多一个依赖 —— DESIGN.md「选型判断」有此权衡。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..config import get_settings


class TaskStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (get_settings().db_abs_path.parent / "tasks.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict] = {}
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
            except Exception:  # noqa: BLE001  # 损坏行跳过（追加日志的容错）
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
