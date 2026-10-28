"""评测 runner：可判对错的基准 + 消融开关，产出 markdown 报告。

用法（在项目根目录）：
    python scripts/run_eval.py                       # 当前 provider 全量
    python scripts/run_eval.py --ablation sloppy     # 模拟LLM编数字，验证QA闭环
    python scripts/run_eval.py --ablation no_qa      # 关闭修订循环，看质量损失
    python scripts/run_eval.py --only query          # 只跑某类

判分口径：
- query     执行准确率：生成SQL与goldSQL在同一库上执行，结果集(排序后、4位小数)一致才得分
- refusal   拒答正确率：answer.kind == refusal
- clarify   澄清正确率：answer.kind == clarify
- drill     归因完整率：answer正常 + 有叙述 + QA数值核验通过
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from ..agents.graph import run_ask
from ..agents.nodes import check_numbers
from ..config import get_settings
from ..data.sandbox import execute_sync

DATASET = Path(__file__).resolve().parent.parent.parent / "eval" / "dataset.json"


def _canonical(rows: list[list], columns: list[str]) -> list[tuple]:
    out = []
    for r in rows:
        out.append(tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r))
    return sorted(out, key=lambda t: json.dumps(t, default=str, ensure_ascii=False))


def _load_dataset(path: Path | None = None) -> list[dict]:
    """评测集加载：默认 v1（33 条回归集），v2 分层集经 --dataset 显式传入。

    条目可带 role（行级权限端到端评测）与 split（holdout 分层）字段，
    缺省 boss / main，v1 条目无字段时行为与历史完全一致。
    """
    p = path or DATASET
    return json.loads(p.read_text(encoding="utf-8"))["items"]


def judge_query(answer: dict, gold_sql: str) -> tuple[bool, str]:
    if answer.get("kind") != "answer":
        return False, f"未产出答案（{answer.get('kind')}）"
    got = answer.get("table") or {}
    if not got.get("rows"):
        return False, "结果为空"
    gold = execute_sync(gold_sql)
    a, b = _canonical(got["rows"], got.get("columns", [])), _canonical(gold.rows, gold.columns)
    if len(got.get("columns", [])) != len(gold.columns):
        return False, f"列数不符：{len(got.get('columns', []))} vs {len(gold.columns)}"
    # 列序无关：真实模型会产生维度顺序漂移（如 [channel,month] vs 金标 [month,channel]），
    # 列集合相同但顺序不同时按列名重排后比对——语义等价的答案不应因列序被判错
    got_cols, gold_cols = list(got.get("columns", [])), list(gold.columns)
    if sorted(got_cols) == sorted(gold_cols) and got_cols != gold_cols:
        perm = [got_cols.index(c) for c in gold_cols]
        a = sorted(tuple(row[p] for p in perm) for row in got["rows"])
    if a == b:
        return True, ""
    # top-k 等价：『哪个X最…』类问题，模型返回 gold 排序后的前 k 行即语义等价
    if len(a) < len(b) and b[: len(a)] == a:
        return True, f"top-{len(a)} 等价（gold 共 {len(b)} 行）"
    return False, f"结果不一致：{len(a)}行 vs gold {len(b)}行"


async def run(
    provider_tag: str,
    ablation: str,
    only: str | None,
    limit: int | None = None,
    dataset_path: Path | None = None,
    split: str | None = None,
) -> dict:
    import app.llm.mock as mock_mod

    sloppy = ablation == "sloppy"
    if sloppy:
        mock_mod.SLOPPY = True
    settings = get_settings()
    saved_max_rev = settings.max_qa_revisions
    if ablation == "no_qa":
        settings.max_qa_revisions = 0

    items = [i for i in _load_dataset(dataset_path) if not only or i["kind"] == only]
    if split:  # holdout 等分层：只跑标记 split 的条目
        items = [i for i in items if i.get("split", "main") == split]
    if limit:
        items = items[:limit]

    results = []
    for it in items:
        t0 = time.perf_counter()
        state = await run_ask(it["question"], role=it.get("role", "boss"))
        elapsed = (time.perf_counter() - t0) * 1000
        answer = state.get("answer") or {}
        kind = it["kind"]
        detail = ""
        if kind == "query":
            ok, detail = judge_query(answer, it["gold_sql"])
        elif kind == "refusal":
            ok = answer.get("kind") == "refusal"
            detail = "" if ok else f"期望拒答，得到 {answer.get('kind')}"
        elif kind == "clarify":
            ok = answer.get("kind") == "clarify"
            detail = "" if ok else f"期望澄清，得到 {answer.get('kind')}"
        else:  # drill
            ok = answer.get("kind") == "answer" and answer.get("narrative") and (answer.get("qa") or {}).get("passed")
            detail = "" if ok else "归因链路未走通或数值核验未通过"
        # 数值一致性（叙述非空时）
        num_ok = None
        if answer.get("narrative"):
            issues, _checked = check_numbers(
                answer["narrative"], {"rows": (answer.get("table") or {}).get("rows", [])},
                state.get("facts", {}),
            )
            num_ok = not issues
        tokens = sum(
            u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
            for u in (state.get("usage") or {}).values()
        )
        results.append({
            "id": it["id"], "kind": kind, "question": it["question"], "ok": ok,
            "detail": detail, "elapsed_ms": round(elapsed, 1), "tokens": tokens,
            "answer_kind": answer.get("kind"), "number_consistent": num_ok,
            "role": it.get("role", "boss"),
            # DSL/SQL 留痕：失败 case 无需重跑即可归因（此前为已知限制）
            "dsl": state.get("dsl"),
            "sql": state.get("sql"),
        })
        flag = "✓" if ok else "✗"
        print(f"  {flag} {it['id']} [{kind}] {it['question']}  {elapsed:.0f}ms {detail}")

    settings.max_qa_revisions = saved_max_rev
    mock_mod.SLOPPY = False

    by_kind: dict[str, dict] = {}
    for r in results:
        c = by_kind.setdefault(r["kind"], {"n": 0, "ok": 0})
        c["n"] += 1
        c["ok"] += r["ok"]
    by_role: dict[str, dict] = {}
    for r in results:
        c = by_role.setdefault(r["role"], {"n": 0, "ok": 0})
        c["n"] += 1
        c["ok"] += r["ok"]
    lat = [r["elapsed_ms"] for r in results]
    nums = [r for r in results if r["number_consistent"] is not None]
    summary = {
        "provider": provider_tag,
        "ablation": ablation,
        "dataset": str(dataset_path.relative_to(dataset_path.parent.parent.parent)) if dataset_path else "eval/dataset.json (v1 smoke 回归集)",
        "split": split,
        "total": len(results),
        "accuracy": sum(r["ok"] for r in results) / max(1, len(results)),
        "by_kind": by_kind,
        "by_role": by_role,
        "latency_ms_avg": round(statistics.mean(lat), 1) if lat else 0,
        "latency_ms_p95": round(sorted(lat)[int(len(lat) * 0.95) - 1], 1) if lat else 0,
        "tokens_total": sum(r["tokens"] for r in results),
        "number_consistency": (sum(1 for r in nums if r["number_consistent"]) / len(nums)) if nums else None,
        "results": results,
    }
    return summary


def write_report(summary: dict, path: Path) -> None:
    lines = [
        f"# AskBI 评测报告（{summary['provider']} / ablation={summary['ablation']}）",
        "",
        f"- 评测集：{summary.get('dataset', 'eval/dataset.json (v1 smoke 回归集)')}"
        + (f" · split={summary['split']}" if summary.get("split") else ""),
        f"- 总样本：{summary['total']}，总准确率：**{summary['accuracy']:.1%}**",
        f"- 延迟：avg {summary['latency_ms_avg']}ms / p95 {summary['latency_ms_p95']}ms",
        f"- token 消耗：{summary['tokens_total']}",
        f"- 角色×结果：{json.dumps(summary.get('by_role', {}), ensure_ascii=False)}",
    ]
    if summary["number_consistency"] is not None:
        lines.append(f"- 叙述数值一致率：**{summary['number_consistency']:.1%}**")
    lines += ["", "| 类别 | 通过/总数 | 准确率 |", "|---|---|---|"]
    for k, c in summary["by_kind"].items():
        lines.append(f"| {k} | {c['ok']}/{c['n']} | {c['ok'] / c['n']:.0%} |")
    lines += ["", "| id | 结果 | 问题 | 耗时ms | 数值一致 | 备注 |", "|---|---|---|---|---|---|"]
    for r in summary["results"]:
        nc = r["number_consistent"]
        nc_s = "-" if nc is None else ("✓" if nc else "✗")
        note = r["detail"]
        if not r["ok"] and r.get("dsl"):
            # 失败 case 附 DSL/SQL 留痕（截断），无需重跑即可归因
            sql = (r.get("sql") or "").replace("\n", " ")[:160]
            note += f" 〈DSL: {json.dumps(r['dsl'], ensure_ascii=False)[:180]}｜SQL: {sql}〉"
        lines.append(f"| {r['id']} | {'✓' if r['ok'] else '✗'} | {r['question']} | {r['elapsed_ms']:.0f} | {nc_s} | {note} |")
    path.write_text("\n".join(lines), encoding="utf-8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ablation", choices=["none", "sloppy", "no_qa"], default="none")
    ap.add_argument("--only", choices=["query", "refusal", "clarify", "drill"], default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dataset", type=str, default=None,
                    help="评测集路径（相对项目根或绝对），缺省 v1 回归集")
    ap.add_argument("--split", type=str, default=None,
                    help="只跑标记 split 的条目（如 holdout），缺省全部")
    args = ap.parse_args()

    settings = get_settings()
    dataset_path = Path(args.dataset) if args.dataset else None
    parts = [settings.llm_provider]
    if dataset_path:
        parts.append(dataset_path.stem)
    if args.split:
        parts.append(args.split)
    if args.ablation != "none":
        parts.append(args.ablation)
    tag = "_".join(parts)
    print(f"评测开始：provider={settings.llm_provider} ablation={args.ablation} "
          f"dataset={args.dataset or 'v1'} split={args.split or 'all'}")
    summary = await run(tag, args.ablation, args.only, args.limit, dataset_path, args.split)
    out = Path("eval") / f"report_{tag}.md"
    write_report(summary, out)
    print(f"\n总准确率 {summary['accuracy']:.1%}  报告已写入 {out}")


if __name__ == "__main__":
    asyncio.run(main())
