"""PROFILE=full 全特性命令行演示（mock 模式，零 API 花费）。

四个场景，对应 README「去 demo 化」后的完整能力面：
  1. 多轮指代消解  ——「那华北呢」继承上轮查询骨架（app/context）
  2. 查询缓存回放  ——同 DSL 同角色复问，第二次命中缓存不落沙箱（app/cache）
  3. 澄清挂起/恢复 ——interrupt 挂起等用户，Command(resume) 续跑出数（E4②）
  4. 行级权限三角色 ——boss/south/north 同题不同数，权限在编译期注入

用法（在 askbi/ 项目根目录）：
    python scripts/demo_full.py
前提：data/askbi.duckdb 已生成（python scripts/gen_data.py）。
"""

from __future__ import annotations

import asyncio
import os

# ---- 环境硬前置：必须在任何 app 导入之前（.env 不参与，防误烧 key）----
os.environ["PROFILE"] = "full"
os.environ["LLM_PROVIDER"] = "mock"
# 一次性 run_ask 路径不携带 thread_id，带 checkpointer 的图跑不了；
# 挂起语义由场景 3 的独立挂起图（build_graph + Command(resume)）演示，与 API 同构。
os.environ.setdefault("CLARIFY_SUSPEND_ENABLED", "0")

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.agents.graph import build_graph  # noqa: E402
from app.cache import get_query_cache  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.context import get_session_context, resolve_followup  # noqa: E402

LINE = "=" * 64


def tokens(usage: dict) -> int:
    return sum(u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
               for u in (usage or {}).values())


def show_answer(state: dict, note: str = "") -> None:
    a = state.get("answer", {})
    badge = a.get("data_badge") or {}
    if note:
        print(f"  ↳ 消解：{note}")
    print(f"  ↳ 答案：{a.get('summary', a.get('message', ''))}")
    if a.get("narrative"):
        print(f"  ↳ 叙述：{a['narrative'][:120]}")
    hits = [e for e in state.get("events", []) if e["type"] in ("cache_hit", "cache_store")]
    for e in hits:
        print(f"  ↳ 事件：[{e['node']}] {e['type']} key={e['data']['key']}…")
    if badge:
        print(f"  ↳ 徽章：from_cache={badge.get('from_cache')} "
              f"rows={badge.get('row_count')} elapsed={badge.get('elapsed_ms')}ms")
    print(f"  ↳ 成本：{tokens(state.get('usage'))} tokens · {len(state.get('usage', {}))} 个 LLM 节点")


async def demo_followup() -> None:
    print(f"\n{LINE}\n场景 1 · 多轮指代消解（followup_enabled={get_settings().followup_enabled}）\n{LINE}")
    sid, ctx = "demo-followup", get_session_context()
    ctx.clear(sid)
    for raw in ("上月华南地区的毛利率", "那华北呢"):
        question, note = resolve_followup(raw, ctx.get(sid), enabled=True)
        ctx.set(sid, question)
        print(f"\n👤 {raw}" + (f"   （改写 → {question}）" if question != raw else ""))
        state = await _ask(question)
        show_answer(state, note)


async def _ask(question: str, role: str = "boss") -> dict:
    from app.agents.graph import run_ask
    return await run_ask(question, role=role)


async def demo_cache() -> None:
    print(f"\n{LINE}\n场景 2 · 查询缓存回放（cache_enabled={get_settings().cache_enabled}）\n{LINE}")
    cache = get_query_cache()
    cache.clear()
    before = cache.stats()          # 计数器跨场景累计，只展示本场景增量
    q = "上月美妆品类的销售额"
    for i in (1, 2):
        print(f"\n👤（第 {i} 遍）{q}")
        state = await _ask(q)
        show_answer(state)
    after = cache.stats()
    d = {k: after[k] - before[k] for k in ("hits", "misses", "stores")}
    print(f"\n📊 本场景缓存计数：hits={d['hits']} misses={d['misses']} stores={d['stores']} "
          f"（进程累计 hit_rate={after['hit_rate']:.0%}）")


async def demo_suspend() -> None:
    print(f"\n{LINE}\n场景 3 · 澄清挂起/恢复（interrupt 语义，E4②）\n{LINE}")
    graph = build_graph(clarify_suspend=True, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "demo-suspend"}}   # langgraph 会改写 config，每次新建

    print("\n👤 最近卖得怎么样")
    payload = None
    async for chunk in graph.astream(
            {"question": "最近卖得怎么样", "role": "boss", "events": [], "usage": {}},
            config=cfg, stream_mode="updates"):
        if "__interrupt__" in chunk:
            payload = chunk["__interrupt__"][0].value
    print(f"  ⏸️ 图已挂起，反问：{payload['question']}  选项：{payload.get('options')}")

    answer_text = "销售额"
    print(f"\n👤（澄清）{answer_text}   ← Command(resume=...) 续跑，不重跑已完成的节点")
    # stream_mode="updates" 只给节点增量；节点返回的 usage/events 均为全量累计值，取最新即可
    acc: dict = {"answer": None, "events": [], "usage": {}}
    async for chunk in graph.astream(Command(resume=answer_text), config=cfg,
                                     stream_mode="updates"):
        for delta in chunk.values():
            if not isinstance(delta, dict):
                continue
            if delta.get("answer"):
                acc["answer"] = delta["answer"]
            if delta.get("usage"):
                acc["usage"] = delta["usage"]
            if delta.get("events"):
                acc["events"] = delta["events"]
    merged = (acc["events"] and [e for e in acc["events"]
                                 if e["type"] == "clarify_resolved"] or [{}])[0]
    merged_q = f"最近卖得怎么样（用户澄清：{answer_text}）"
    if merged:
        merged_q = f"最近卖得怎么样（用户澄清：{merged.get('data', {}).get('answer', answer_text)}）"
    print(f"  ↳ 续跑问题：{merged_q}")
    show_answer(acc)


async def demo_roles() -> None:
    print(f"\n{LINE}\n场景 4 · 行级权限三角色（同题不同数，编译期注入）\n{LINE}")
    q = "上月各区域销售额"
    for role, label in (("boss", "老板·全区域"), ("south", "华南运营·仅华南"), ("north", "北方区·华北+东北")):
        state = await _ask(q, role=role)
        a = state.get("answer", {})
        rows = (a.get("table") or {}).get("rows", [])
        detail = "、".join(f"{r[0]}={r[1]:.0f}" for r in rows[:4])
        print(f"\n👤 [{role}] {q}")
        print(f"  ↳ {a.get('summary', '')}")
        print(f"  ↳ 口径：{a.get('caliber', '')}")
        print(f"  ↳ 数据：{len(rows)} 行 → {detail}{'…' if len(rows) > 4 else ''}")


async def main() -> None:
    s = get_settings()
    if not s.db_abs_path.exists():
        raise SystemExit(f"数据文件缺失：{s.db_abs_path}（先运行 python scripts/gen_data.py）")
    print(f"AskBI PROFILE=full 演示 · provider={s.llm_provider}（零 API 花费）· "
          f"cache={s.cache_enabled} followup={s.followup_enabled} · "
          f"澄清挂起：场景 3 独立图演示（一次性路径不含 thread_id）")
    await demo_followup()
    await demo_cache()
    await demo_suspend()
    await demo_roles()
    print(f"\n{LINE}\n演示结束：四场景全部走通（mock 规则引擎，输出确定性可复现）\n{LINE}")


if __name__ == "__main__":
    asyncio.run(main())
