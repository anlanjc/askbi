"""多轮评测 runner（eval v2 追加集）：30 条 × 64 轮，mock 可判定、零 API 花费。

与 app/eval/runner.py（v1 单轮回归集）的分工：
- 判分器复用 app.eval.runner.judge_query（同一把尺子），不修改 runner.py；
- 多轮接线在本脚本层镜像 app/api/main.py /api/ask 的顺序：resolve_followup →
  ctx.set(改写后问题) → run_ask，保证"评测跑的就是线上行为"；
- 澄清挂起条目（item.suspend=true）走 build_graph(clarify_suspend=True) + Command(resume)，
  与 API 的挂起拓扑同构——run_ask 的一次性路径不携带 thread_id，挂不起也恢复不了。

环境（在导入 app 之前强制写死，.env 不参与，防误烧 key）：
- PROFILE=full        → cache_enabled / followup_enabled 开（被评测的两个实验特性）
- LLM_PROVIDER=mock   → 零 API 花费
- CLARIFY_SUSPEND_ENABLED=0 → 一次性 run_ask 路径没有 thread_id；挂起语义由
  suspend 条目经独立图验证（配置约定：显式配置优先于档位默认）

用法（在 askbi/ 项目根目录）：
    python scripts/run_multiturn_eval.py                     # 全量 → evidence/multiturn_report.md
    python scripts/run_multiturn_eval.py --only cache_repeat  # 只跑某场景
    python scripts/run_multiturn_eval.py --out path/to.md    # 自定义报告路径

前提：data/askbi.duckdb 已生成（python scripts/gen_data.py，数据文件缺失时直接报错退出，
不自动重生成——保持评测输入不可变）。
"""

from __future__ import annotations

import os

# ---- 环境硬前置：必须在任何 app 导入之前 ----
os.environ["PROFILE"] = "full"
os.environ.setdefault("LLM_PROVIDER", "mock")   # 默认 mock；显式传 LLM_PROVIDER=openai_compatible 跑真模型
os.environ.setdefault("CLARIFY_SUSPEND_ENABLED", "0")

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.graph import build_graph, run_ask
from app.cache import get_query_cache
from app.config import get_settings
from app.context import get_session_context, resolve_followup
from app.eval.runner import judge_query  # 复用 v1 判分器（只导入不修改）

DATASET = ROOT / "eval" / "dataset_v2_followup.json"
DEFAULT_REPORT = ROOT.parent / "evidence" / "multiturn_report.md"


def load_dataset(path: Path) -> list[dict]:
    """加载评测集：.json（{"items":[...]}）或 .jsonl（每行一条会话）。

    条目数断言只约束默认的 v2 追加集（历史口径钉死 30 条）；外部数据集
    （如 [1B4-AB] multiturn_deep.jsonl）按文件实际条数跑。
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        # 每行一条会话；跳过元信息行（无 id/turns 的行，如首行 _note）
        items = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        items = [i for i in items if isinstance(i, dict) and i.get("id") and i.get("turns")]
    else:
        items = json.loads(text)["items"]
    if path == DATASET and len(items) != 30:
        raise SystemExit(f"评测集应为 30 条，实际 {len(items)} 条：{path}")
    return items


async def run_turn(turn: dict, item: dict, sid: str, graph, settings) -> dict:
    """跑一轮：镜像 API 接线（resolve → ctx.set → 执行），返回判定所需信息。"""
    role = turn.get("role", item.get("role", "boss"))
    raw = turn["q"]
    question, note = resolve_followup(raw, get_session_context().get(sid),
                                      enabled=settings.followup_enabled)
    get_session_context().set(sid, question)   # 与 API 一致：存改写后的完整问题作下轮上下文

    suspended_payload = None
    merged_question = ""
    if item.get("suspend"):
        # langgraph 会原地改写 config dict——每次 astream 都新建（tests/test_clarify_suspend 实测踩坑）
        cfg = {"configurable": {"thread_id": sid}}
        # [1B4-AB] 任务四修复：非 resume 轮的 payload 必须与 API 同构携带 depth=""
        # （AskRequest 默认值）——曾漏传该键，thread 的 depth 通道保留上一轮路由值，
        # 挂起 thread 上的后续新问题被首轮路由冻结（light 后再问"为什么…"也走轻量
        # 链路，叙述与 QA 永不产出）。API 每请求都显式带 depth，评测装置漂移当日修。
        payload = Command(resume=question) if turn.get("resume") else {
            "question": question, "role": role, "depth": "", "events": [], "usage": {}}
        last: dict = {}
        async for chunk in graph.astream(payload, config=cfg, stream_mode="updates"):
            if "__interrupt__" in chunk:
                intr = chunk["__interrupt__"][0]
                suspended_payload = intr.value if isinstance(intr.value, dict) else {}
                last.update(chunk)
                break
            last.update(chunk)
        answer = (last.get("finalizer") or {}).get("answer") or {}
        merged_question = (last.get("clarify_suspend") or {}).get("question", "")
        usage, events = last.get("usage", {}), last.get("events", [])
    else:
        t0 = time.perf_counter()
        state = await run_ask(question, role=role)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        answer = state.get("answer") or {}
        usage, events = state.get("usage", {}), state.get("events", [])

    return {
        "raw": raw, "resolved": question, "note": note, "role": role,
        "answer": answer, "usage": usage, "events": events,
        "suspended_payload": suspended_payload, "merged_question": merged_question,
        "elapsed_ms": elapsed_ms if not item.get("suspend") else None,
    }


def judge_turn(r: dict, turn: dict) -> tuple[bool, str]:
    """单轮判定：kind + gold 结果集 + 缓存徽章 + 消解说明 + 挂起负载。"""
    exp = turn["expect"]
    kind = exp["kind"]
    answer = r["answer"]
    if kind == "suspend":
        if r["suspended_payload"] is None:
            return False, "期望澄清挂起，但未发生 interrupt"
        if not r["suspended_payload"].get("question"):
            return False, "挂起负载缺少反问内容"
        return True, ""
    if answer.get("kind") != kind:
        return False, f"期望 {kind}，得到 {answer.get('kind')}"
    if kind == "answer":
        ok, detail = judge_query(answer, exp["gold_sql"])
        if not ok:
            return False, detail
    fc = exp.get("from_cache")
    if fc is not None:
        got_fc = bool((answer.get("data_badge") or {}).get("from_cache"))
        if got_fc != fc:
            return False, f"期望 from_cache={fc}，实际 {got_fc}"
    for frag in exp.get("note_contains", []):
        if frag not in r["note"]:
            return False, f"消解说明缺少「{frag}」：{r['note'] or '<空>'}"
    merged = exp.get("merged_contains")
    if merged and merged not in r["merged_question"]:
        return False, f"恢复后问题未并入澄清内容（{merged}）"
    return True, ""


async def run(only: str | None, dataset_path: Path | None = None) -> dict:
    settings = get_settings()
    ds_abs = (dataset_path or DATASET).resolve()
    items = [i for i in load_dataset(ds_abs) if not only or i["scenario"] == only]
    cache = get_query_cache()
    ctx = get_session_context()

    results: list[dict] = []
    for item in items:
        cache.clear()                      # 条目间清缓存：命中判定只看条目内轮次，与条目顺序无关
        sid = f"eval-{item['id']}"
        ctx.clear(sid)
        graph = build_graph(clarify_suspend=True, checkpointer=InMemorySaver()) \
            if item.get("suspend") else None
        item_ok = True
        for tno, turn in enumerate(item["turns"], 1):
            t0 = time.perf_counter()
            r = await run_turn(turn, item, sid, graph, settings)
            ok, detail = judge_turn(r, turn)
            item_ok &= ok
            tokens = sum(u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
                         for u in (r["usage"] or {}).values())
            from_cache = bool((r["answer"].get("data_badge") or {}).get("from_cache")) \
                if r["answer"].get("kind") == "answer" else None
            results.append({
                "item": item["id"], "turn": tno, "scenario": item["scenario"],
                "role": r["role"], "raw": r["raw"], "resolved": r["resolved"],
                "ok": ok, "detail": detail, "kind": r["answer"].get("kind"),
                "from_cache": from_cache, "note": r["note"],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "tokens": tokens,
            })
            flag = "✓" if ok else "✗"
            q_show = r["resolved"] if r["resolved"] != r["raw"] else r["raw"]
            extra = " [缓存命中]" if from_cache else ""
            print(f"  {flag} {item['id']}.t{tno} [{item['scenario']}] {q_show}{extra} "
                  f"{detail}" + ("" if ok else f"  note={r['note']!r}"))

    turns = len(results)
    ok_turns = sum(r["ok"] for r in results)
    items_all = len(items)
    ok_items = len({r["item"] for r in results if r["ok"]} &
                   {i["id"] for i in items if all(rr["ok"] for rr in results
                                                  if rr["item"] == i["id"])})
    by_scenario: dict[str, dict] = {}
    for r in results:
        c = by_scenario.setdefault(r["scenario"], {"n": 0, "ok": 0})
        c["n"] += 1
        c["ok"] += r["ok"]
    cache_hits = sum(1 for r in results if r["from_cache"])
    lat = [r["elapsed_ms"] for r in results]
    summary = {
        "provider": settings.llm_provider, "profile": settings.profile,
        "dataset": ds_abs.relative_to(ROOT).as_posix() if ds_abs.is_relative_to(ROOT) else ds_abs.name,
        "only": only, "items": items_all, "items_all_correct": ok_items,
        "turns": turns, "turn_accuracy": ok_turns / max(1, turns),
        "by_scenario": by_scenario, "cache_hit_turns": cache_hits,
        "cache_stats": cache.stats(),
        "latency_ms_avg": round(statistics.mean(lat), 1) if lat else 0,
        "tokens_total": sum(r["tokens"] for r in results),
        "results": results,
    }
    return summary


def write_report(s: dict, path: Path) -> None:
    lines = [
        f"# AskBI 多轮评测报告（{s['dataset']} · {s['provider']}）",
        "",
        f"- 评测集：`{s['dataset']}`" + (f"（--only {s['only']}）" if s["only"] else ""),
        f"- 环境：provider={s['provider']}"
        + ("（零 API 花费）" if s["provider"] == "mock" else "")
        + f" · PROFILE={s['profile']}"
        "（cache/followup 开）· 挂起条目走独立 checkpointer 图",
        (f"- 结果：条目全对 **{s['items_all_correct']}/{s['items']}** · "
        f"轮次正确 **{round(s['turn_accuracy'] * len(s['results']))}/{s['turns']}**"
        f"（{s['turn_accuracy']:.1%}）· 缓存命中轮 {s['cache_hit_turns']}"),
        (f"- 延迟 avg {s['latency_ms_avg']}ms · token ≈{s['tokens_total']} · "
        f"缓存计数 {json.dumps({k: v for k, v in s['cache_stats'].items() if k in ('hits', 'misses', 'stores')}, ensure_ascii=False)}"),
        "",
        "| 场景 | 轮次通过 |",
        "|---|---|",
    ]
    for k, c in s["by_scenario"].items():
        lines.append(f"| {k} | {c['ok']}/{c['n']} |")
    lines += ["", "| 条目.轮 | 结果 | 场景 | 角色 | 问（消解后） | 期望 | 缓存 | 消解说明/失败详情 |", "|---|---|---|---|---|---|---|---|"]
    for r in s["results"]:
        note = r["note"] or ""
        text = f"{r['detail']} note={note}" if not r["ok"] else (note or r["detail"])
        lines.append(
            f"| {r['item']}.t{r['turn']} | {'✓' if r['ok'] else '✗'} | {r['scenario']} "
            f"| {r['role']} | {r['resolved']} | {r['kind']}"
            f"{'/cache' if r['from_cache'] else ''} | {'⚡' if r['from_cache'] else '-'} | {text} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某 scenario（如 cache_repeat）")
    ap.add_argument("--dataset", default=None,
                    help="评测集路径（.json 或 .jsonl），缺省 eval/dataset_v2_followup.json（30 条口径）")
    ap.add_argument("--out", default=None, help="报告输出路径，缺省随 --dataset 推导")
    args = ap.parse_args()

    dataset_path = Path(args.dataset) if args.dataset else DATASET
    if args.out:
        out = Path(args.out)
    elif dataset_path == DATASET:
        out = DEFAULT_REPORT
    else:
        out = ROOT / "evidence" / f"multiturn_{dataset_path.stem}_report.md"

    if not get_settings().db_abs_path.exists():
        raise SystemExit(f"数据文件缺失：{get_settings().db_abs_path}（先运行 python scripts/gen_data.py）")

    print(f"多轮评测开始：{get_settings().llm_provider} / PROFILE={get_settings().profile} / dataset={dataset_path.name}")
    summary = await run(args.only, dataset_path)
    write_report(summary, out)
    acc = summary["turn_accuracy"]
    print(f"\n条目全对 {summary['items_all_correct']}/{summary['items']} · "
          f"轮次正确率 {acc:.1%} · 缓存命中轮 {summary['cache_hit_turns']} · 报告已写入 {out}")
    if acc < 1.0:
        raise SystemExit("FAIL：存在未通过轮次，见上方 ✗ 明细")
    print("PASS：多轮评测全部通过")


if __name__ == "__main__":
    asyncio.run(main())
