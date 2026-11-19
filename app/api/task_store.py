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

# ASK-9：内存索引上限——淘汰仅影响常驻查询窗口，JSONL 全量留盘可回放
_MAX_IN_MEMORY = 200

# AB-18②：resume 在途标记 TTL——超过此占用时长视为"生成器未启动窗口"的死标记
# （starlette 首帧 send 前不保证首迭代生成器、从不 aclose StreamingResponse——
# gen 未创建即取消时 finally 不跑，标记永久泄漏），下次 acquire 时收回。
# 默认上界 = max(sql_timeout) + LLM 预算：一次合法 astream 的最长在途时长
# （SQL 超时上界 + 单次 LLM 请求上界 120s（app/llm/deepseek.py）+ 连接余量 10s）；
# 构造参数 resume_ttl_seconds 显式覆盖优先（config 不新增字段的折中：TTL 语义
# 属 TaskStore 内部纪律，非部署面配置）。误收回的最坏后果 = 同 thread 可再占
# （退回 ASKBI-7 修复前的并发窗口，有属主校验与任务终态兜底），故上界取宽不取窄。
_LLM_REQUEST_BUDGET_SECONDS = 130.0


def _default_resume_ttl_seconds() -> float:
    s = get_settings()
    return max(float(s.sql_timeout_seconds), 0.0) + _LLM_REQUEST_BUDGET_SECONDS


class TaskStore:
    def __init__(self, path: Path | None = None, resume_ttl_seconds: float | None = None):
        self.path = path or (get_settings().db_abs_path.parent / "tasks.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()   # 守护管理面复合变更窗口（见 clear）
        # AB-HR2：thread_id → 属主哈希注册表（仅属主隔离开启且有挂起拓扑时写入）。
        # 与 InMemorySaver checkpointer 同生命周期：挂起的 thread 本就不跨重启，
        # 内存态注册表不引入新的持久化面。FIFO 上限防长跑无界增长（ASK-9 同思路）。
        self._thread_owners: dict[str, str | None] = {}
        # ASKBI-7（评审 4.0）：resume 在途标记——thread_id → (占用起点 monotonic, 绑定任务 id)。
        # 占用窗口 = 校验挂起通过 → astream 结束（gen finally 或终态联动释放，见下）。
        # AB-18②（评审 5.0）：SSE 生成器存在"未启动窗口"——starlette 首帧 send 前不保证
        # 首迭代生成器、从不 aclose StreamingResponse，gen 未创建即取消时 finally 不跑，
        # 标记曾永久泄漏（resume 恒 409）、绑定任务永驻 running 绕过 _evict_if_over。
        # 条目带单调时间戳：acquire 对超龄占用（> resume_ttl_seconds）收回 + 留痕计数，
        # 并联动把滞留任务落 aborted 终态。纯内存态：与挂起拓扑 checkpointer 同生命周期。
        self._resuming_threads: dict[str, tuple[float, str | None]] = {}
        self._resume_ttl_seconds = (
            _default_resume_ttl_seconds() if resume_ttl_seconds is None else resume_ttl_seconds
        )
        self._resume_ttl_reclaims = 0   # AB-18②：TTL 收回留痕计数（评审可观测）
        self._load()

    # ---- AB-HR2 thread 属主注册表 ----

    _MAX_THREAD_OWNERS = 4096

    def bind_thread_owner(self, thread_id: str, owner: str | None) -> None:
        """挂起 thread 建立时登记属主。setdefault 语义：已有绑定不覆盖——
        防止攻击者以同 thread_id 重新提问改写属主（绑定劫持）。"""
        if not thread_id:
            return
        with self._lock:
            if len(self._thread_owners) >= self._MAX_THREAD_OWNERS and thread_id not in self._thread_owners:
                self._thread_owners.pop(next(iter(self._thread_owners)), None)
            self._thread_owners.setdefault(thread_id, owner)

    def thread_owner(self, thread_id: str) -> str | None:
        """未登记的 thread 返回 None（fail-closed：无法证明属主）。"""
        with self._lock:
            return self._thread_owners.get(thread_id)

    # ---- ASKBI-7（评审 4.0）resume 在途标记（AB-18：TTL 收回 + 终态联动释放） ----

    @property
    def resume_ttl_reclaims(self) -> int:
        """AB-18② 留痕：超龄占用被 TTL 收回的次数（评审/运维可观测）。"""
        return self._resume_ttl_reclaims

    def acquire_thread_resume(self, thread_id: str, task_id: str | None = None) -> bool:
        """per-thread 在途占用（setdefault 语义）：第一个 caller 占用成功返回 True；
        同 thread 其余并发 caller 得 False（API 层据此 409 拒绝第二个并发 run——
        曾两个 resume 都能通过 snap.next 检查并各自恢复同一挂起 thread）。

        task_id 绑定本占用：①终态联动释放（finish/finish_if_running 按绑定反查）；
        ②TTL 收回时联动把滞留 running 的绑定任务落 aborted 终态。
        AB-18②：条目带单调时间戳——占用超过 resume_ttl_seconds 视为"生成器未启动
        窗口"的死标记（gen 未创建即取消时 finally 不跑），本次 acquire 收回 + 留痕，
        新鲜占用不受影响（不误伤在途 run）。
        """
        now = time.monotonic()
        stale_task: str | None = None
        with self._lock:
            entry = self._resuming_threads.get(thread_id)
            if entry is not None and now - entry[0] < self._resume_ttl_seconds:
                return False
            if entry is not None:
                stale_task = entry[1]
                self._resume_ttl_reclaims += 1
            self._resuming_threads[thread_id] = (now, task_id)
            if task_id is not None and task_id in self._tasks:
                self._tasks[task_id]["resume_thread"] = thread_id
        if stale_task is not None:
            # 超龄占用联动终态：任务曾永驻 running 绕过内存上限（running 不可淘汰）。
            # 在锁外调 finish_if_running（同锁非重入）；迟到的真实终态与本兜底幂等。
            self.finish_if_running(
                stale_task,
                {"kind": "aborted", "message": "resume 标记超龄未释放，被 TTL 收回（SSE 生成器未启动窗口）"},
                {}, [], 0.0,
            )
        return True

    def release_thread_resume(self, thread_id: str, task_id: str | None = None) -> None:
        """释放 resume 在途占用（SSE 生成器 finally 必经 + 终态联动释放共用）。幂等。

        task_id 提供时做属主校验：仅当在途条目仍绑定本任务（或条目无绑定）才释放——
        TTL 收回 + 新 run 接管后，旧 run 迟到的 finally 不得偷走新 run 的标记（否则
        互斥被静默拆除，第二个并发 run 得以放行）。
        """
        with self._lock:
            entry = self._resuming_threads.get(thread_id)
            if entry is None:
                return
            if task_id is not None and entry[1] is not None and entry[1] != task_id:
                return   # 标记已归属新 run：本次为旧 run 的迟到释放，不偷
            del self._resuming_threads[thread_id]

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                t = json.loads(line)
                # ASKBI-1（评审 4.0）：JSONL 里滞留的 running 条目在载入时即标终态——
                # 重启后不可能还有在跑的 SSE（进程都死了），历史僵尸必须可被淘汰，
                # 否则把 ASK-9 的 200 上限永久顶满（载入不淘汰曾使其不可回收）。
                # 内存态标记；JSONL 追加日志保持原样可回放。
                if t.get("status") == "running":
                    t.update(status="done", kind="aborted",
                             answer={"kind": "aborted", "message": "服务重启，任务中断"})
                self._tasks[t["id"]] = t
            except Exception:  # noqa: BLE001, S112  # 损坏行跳过（追加日志的有意容错）
                continue
        self._evict_if_over()   # ASKBI-1：载入同样受 200 上限约束（淘汰此前只在 create 触发）

    def create(self, question: str, role: str, owner: str | None = None) -> dict:
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
        if owner:
            # AB-HR2：属主绑定只在开关开启时存在（owner=SHA-256 哈希）——
            # 开关关时任务记录逐字节零变化（owner 键不出现）。
            t["owner"] = owner
        with self._lock:
            # ASK-3 修复（评审 2.0）：create/finish/set_feedback 曾不取锁，与 clear 的
            # 复合变更窗口竞态——finish 下标直接 KeyError 冲出 SSE 生成器，客户端
            # 只收 start 无终帧无限等待。
            self._tasks[t["id"]] = t
            self._evict_if_over()
        self._append(t)
        return t

    def _evict_if_over(self) -> None:
        """ASK-9 修复（评审 2.0）：内存索引曾只增不减——每任务带全量 events+答案表，
        长跑 RSS 随请求数线性上涨。上限淘汰仅影响常驻查询窗口，JSONL 全量留盘可回放。"""
        limit = _MAX_IN_MEMORY
        if len(self._tasks) <= limit:
            return
        finished = [t for t in self._tasks.values() if t.get("status") != "running"]
        finished.sort(key=lambda t: t.get("created_at", ""))
        for t in finished[: len(self._tasks) - limit]:
            self._tasks.pop(t["id"], None)

    def finish(self, task_id: str, answer: dict, usage: dict, events: list, elapsed_ms: float) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                # ASK-3 修复：任务已被管理面归档 → 幂等视为完成，不抛 KeyError
                return
            t.update(
                status="done",
                kind=(answer or {}).get("kind"),
                answer=answer,
                usage=usage,
                events=events,
                elapsed_ms=round(elapsed_ms, 1),
            )
            resume_thread = t.pop("resume_thread", None)   # AB-18①：绑定随终态拆走
        if resume_thread:
            # AB-18①（评审 5.0）：任一终态路径联动释放在途标记——gen finally 不再是
            # 唯一释放点（"finish 后 finally 前抛错"之类的残余窗口不再泄漏标记）。
            # 属主校验：标记已被 TTL 收回 + 新 run 接管时，此处迟到释放不偷新标记。
            self.release_thread_resume(resume_thread, task_id=task_id)
        self._append(t)

    def finish_if_running(
        self, task_id: str, answer: dict, usage: dict, events: list, elapsed_ms: float
    ) -> bool:
        """终态兜底（ASKBI-1，评审 4.0）：任务仍 running 才落终态并落盘。

        与 finish 的差别：已终态（正常/error/clarify_pending）→ no-op 返回 False。
        供 SSE 生成器的 finally 兜底使用——客户端断连（GeneratorExit）/请求取消
        （CancelledError，均 BaseException，except Exception 接不住）走这里落
        kind=aborted，任务不再永久滞留 running 绕过内存上限。
        AB-18①（评审 5.0）：落终态时联动释放在途标记（同 finish）——任务已死，
        标记必不活过它的任务（gen 未启动窗口之外的第二道防线）。
        """
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None or t.get("status") != "running":
                return False
            t.update(
                status="done",
                kind=(answer or {}).get("kind"),
                answer=answer,
                usage=usage,
                events=events,
                elapsed_ms=round(elapsed_ms, 1),
            )
            resume_thread = t.pop("resume_thread", None)
        if resume_thread:
            self.release_thread_resume(resume_thread, task_id=task_id)
        self._append(t)
        return True

    def set_feedback(self, task_id: str, value: str, comment: str = "") -> dict | None:
        """thumbs 反馈：附着到任务并随任务一起追加落盘（重启可回放）。"""
        with self._lock:
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
