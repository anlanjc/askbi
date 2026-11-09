"""构建扩容评测集（eval/dataset_v2_140.json，AB-HR4）：冻结 v2 116 条逐字保留 + 新增 32 条。

纪律（任务卡 HANDOFF_askbi-hardening.md T4）：
- 冻结的 eval/dataset_v2.json 一个字节不改——本脚本只读它；构建产物内冻结条目
  与源文件**逐 dict 相等**（程序化校验，任何漂移立即报错）；
- 新条目金标一律 compile_sql 生成，禁止手写 SQL；
- 构建期自检：mock 解析器重答每道新 query 题，与金标同库执行比对
  （与 scripts/build_dataset_v2.py 同口径），不通过即拒绝入库；
- 新题与冻结 116 题问题串零重复（程序化断言）；
- 新条目带 batch 字段标注批次来源，冻结条目原样（无该字段）。

用法（项目根，须用 askbi/.venv/Scripts/python.exe）：
    python scripts/build_dataset_v2_140.py            # 构建 + 自检，写入 eval/dataset_v2_140.json
    python scripts/build_dataset_v2_140.py --check    # 只自检 + 完整性校验，不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data.dsl import QueryDSL, compile_sql
from app.data.sandbox import execute_sync
from app.llm.mock import _parse_question

FROZEN = ROOT / "eval" / "dataset_v2.json"
TARGET = ROOT / "eval" / "dataset_v2_140.json"


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict:
    """意图 DSL 简写构造器（与 build_dataset_v2.py 同形）。"""
    out: dict = {
        "intent": "aggregate",
        "metric": metric,
        "dimensions": dims or [],
        "filters": [{"field": f, "op": "in" if isinstance(v, list) else "=", "value": v}
                    for f, v in (filters or [])],
        "time": time or {"mode": "all"},
        "order_by": {"metric": metric, "direction": direction or "desc"},
        "limit": limit,
    }
    return out


L1 = {"mode": "last_n_months", "n": 1}
L3 = {"mode": "last_n_months", "n": 3}
L6 = {"mode": "last_n_months", "n": 6}
L12 = {"mode": "last_n_months", "n": 12}
BT = lambda s, e: {"mode": "between", "start": s, "end": e}

# 新增条目：(id, kind, question, dsl | None, role, split)——措辞全部先经 mock 探针
# 验证解析形态再定稿（探针记录见提交信息与 evidence/redteam_report.md 同批报告）。
NEW_SPECS: list[tuple[str, str, str, dict | None, str, str]] = [
    # ---- query ×13：冻结集未覆盖的 指标×维度×时间 组合（服饰/金卡/华中/西北/城市×单量/
    #      细分品类×毛利率趋势/会员等级×退货率 等）----
    ("v2-117", "query", "上个月运动品类的订单量",
     dsl("order_count", filters=[("category", "运动")], time=L1), "boss", "main"),
    ("v2-118", "query", "最近3个月服饰品类的销售额",
     dsl("revenue", filters=[("category", "服饰")], time=L3), "boss", "main"),
    ("v2-119", "query", "上月金卡会员的购买客户数",
     dsl("customer_count", filters=[("member_level", "金卡")], time=L1), "boss", "main"),
    ("v2-120", "query", "2026年1月到2026年6月的净销售额",
     dsl("net_revenue", time=BT("2026-01", "2026-06")), "boss", "main"),
    ("v2-121", "query", "华中地区最近12个月的销售额",
     dsl("revenue", filters=[("region", "华中")], time=L12), "boss", "main"),
    ("v2-122", "query", "上月各会员等级的退货率",
     dsl("refund_rate", dims=["member_level"], time=L1), "boss", "main"),
    ("v2-123", "query", "销售额最低的细分品类",
     dsl("revenue", dims=["subcategory"], direction="asc"), "boss", "main"),
    ("v2-124", "query", "上月订单量排名前5的城市",
     dsl("order_count", dims=["city"], time=L1, limit=5), "boss", "main"),
    ("v2-125", "query", "近3个月各细分品类毛利率趋势",
     dsl("gross_margin_rate", dims=["month", "subcategory"], time=L3), "boss", "main"),
    ("v2-126", "query", "黑卡会员最近12个月的订单量",
     dsl("order_count", filters=[("member_level", "黑卡")], time=L12), "boss", "main"),
    ("v2-127", "query", "上月西北地区各品类销售额",
     dsl("revenue", dims=["category"], filters=[("region", "西北")], time=L1), "boss", "main"),
    ("v2-128", "query", "2024年11月至2025年2月的退货率",
     dsl("refund_rate", time=BT("2024-11", "2025-02")), "boss", "main"),
    ("v2-129", "query", "上月各渠道净销售额",
     dsl("net_revenue", dims=["channel"], time=L1), "north", "main"),
    # ---- drill ×6：归因/分析/对比 新维度组合（含 1 条 south 行级权限 drill）----
    ("v2-130", "drill", "为什么上月服饰品类的订单量下滑了", None, "boss", "main"),
    ("v2-131", "drill", "分析最近6个月各会员等级的客单价", None, "south", "main"),
    ("v2-132", "drill", "为什么上月搜索渠道的毛利额下降了", None, "boss", "main"),
    ("v2-133", "drill", "对比上月各品类的销售额差异", None, "boss", "main"),
    ("v2-134", "drill", "归因一下上月美妆品类的客单价变化", None, "boss", "main"),
    ("v2-135", "drill", "分析上月各细分品类的退货率结构", None, "boss", "main"),
    # ---- clarify ×6：无指标/模糊表述新形态（不与冻结 12 条重复）----
    ("v2-136", "clarify", "业绩怎么样", None, "boss", "main"),
    ("v2-137", "clarify", "上月的情况如何", None, "boss", "main"),
    ("v2-138", "clarify", "整体经营表现如何", None, "boss", "main"),
    ("v2-139", "clarify", "哪个区域卖得最好", None, "boss", "main"),
    ("v2-140", "clarify", "最近哪个品类表现不错", None, "boss", "main"),
    ("v2-141", "clarify", "给我讲讲上个月的数据", None, "boss", "main"),
    ("v2-142", "clarify", "上月哪些东西卖得好", None, "boss", "main"),
    # ---- refusal ×6：预测未来 2 / 未采集指标 1 / 非数据请求 3----
    ("v2-143", "refusal", "帮我预测一下明年的毛利额", None, "boss", "main"),
    ("v2-144", "refusal", "未来半年各渠道的销售额走势如何", None, "boss", "main"),
    ("v2-145", "refusal", "上月NPS评分是多少", None, "boss", "main"),
    ("v2-146", "refusal", "生成一份上月经营总结", None, "boss", "main"),
    ("v2-147", "refusal", "翻译一下这段数据含义", None, "boss", "main"),
    ("v2-148", "refusal", "根据当前数据推荐一下新品类", None, "boss", "main"),
]

BATCH_TAG = "AB-HR4-expansion"


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def self_check_new_queries() -> list[str]:
    """构建期自检：mock 解析器重答每道新 query 题，与金标同库执行比对。"""
    problems: list[str] = []
    for sid, kind, question, spec, role, split in NEW_SPECS:
        if kind != "query":
            continue
        gold_sql, _ = compile_sql(QueryDSL(**spec), role=role)
        try:
            parsed = QueryDSL(**_parse_question(question))
            got_sql, _ = compile_sql(parsed, role=role)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} [{role}] mock 解析/编译失败: {e}\n    Q: {question}")
            continue
        try:
            ok = _canon(execute_sync(got_sql).rows) == _canon(execute_sync(gold_sql).rows)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}")
            continue
        if not ok:
            problems.append(
                f"{sid} [{role}] mock 结果与金标不一致\n    Q: {question}\n    gold: {gold_sql}\n    got : {got_sql}"
            )
    return problems


def integrity_check() -> list[str]:
    """完整性校验：与冻结 116 题问题串零重复；id 连续无冲突。"""
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))["items"]
    frozen_qs = {i["question"].strip() for i in frozen}
    frozen_ids = {i["id"] for i in frozen}
    problems: list[str] = []
    for sid, kind, question, _spec, _role, _split in NEW_SPECS:
        if question.strip() in frozen_qs:
            problems.append(f"{sid} 问题与冻结集重复: {question}")
        if sid in frozen_ids:
            problems.append(f"{sid} id 与冻结集冲突")
    return problems


def build(path: Path | None) -> str:
    frozen_payload = json.loads(FROZEN.read_text(encoding="utf-8"))
    frozen_items = frozen_payload["items"]

    items: list[dict] = [dict(i) for i in frozen_items]   # 逐字保留（dict 层复制，值零改动）
    for sid, kind, question, spec, role, split in NEW_SPECS:
        item: dict = {"id": sid, "kind": kind, "question": question, "role": role, "split": split}
        if spec is not None:
            item["gold_sql"] = compile_sql(QueryDSL(**spec), role=role)[0]
        item["batch"] = BATCH_TAG
        items.append(item)

    # 冻结条目逐 dict 相等校验（防任何漂移）
    for src, out in zip(frozen_items, items[: len(frozen_items)]):
        if src != out:
            raise SystemExit(f"冻结条目漂移: {src.get('id')}")

    from collections import Counter

    stats = Counter((i["kind"], i["split"]) for i in items)
    note = (
        "v2 扩容评测集（AB-HR4，2026-11-09）：冻结 v2 116 条逐字保留 + 新增 32 条 "
        f"（batch={BATCH_TAG}，金标由 compile_sql 生成，scripts/build_dataset_v2_140.py 可重建/自检）。"
        "新条目与冻结集问题串零重复；新 query 题全部通过构建期 mock 重答自检。"
        f"构成：{dict(sorted(stats.items()))}"
    )
    payload = {"note": note, "items": items}
    if path is not None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return note


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只跑自检与完整性校验，不写盘")
    args = ap.parse_args()

    problems = integrity_check()
    problems += self_check_new_queries()
    if problems:
        print(f"自检未通过 {len(problems)} 条：")
        for p in problems:
            print("  -", p)
        return 1
    print("自检通过：新 query 题 mock 重答与金标同库一致；与冻结集零重复、id 无冲突")

    note = build(None if args.check else TARGET)
    print(note)
    if not args.check:
        print(f"已写入 {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
