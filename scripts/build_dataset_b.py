"""构建数据集 B 评测集（eval/dataset_b.json）：金标一律由 compile_sql(spec=B) 生成。

与 build_dataset_v2.py 同款设计，目标数据集换成线下连锁咖啡（app/data/dataset_b.py）：
- query 题内嵌"意图 DSL"，gold_sql = compile_sql(意图DSL, role, spec=coffee) —— 金标与线上编译器同源；
- 构建期自检：mock 解析器（coffee 词表）重答每道 query 题，与金标在同一 DuckDB（B 库）上
  执行比对，不通过即打印差异——措辞超出解析器覆盖/意图 DSL 写错在构建期暴露；
- drill/clarify/refusal 无金标（runner 按 answer kind/叙述+QA 判分）；
- 全部条目 split=main（B 集用于回归验证，无调参流程，不设 holdout）。

用法（项目根）：
    python scripts/build_dataset_b.py            # 构建 + 自检，写入 eval/dataset_b.json
    python scripts/build_dataset_b.py --check    # 只自检不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data.dataset_b import COFFEE_SPEC
from app.data.dsl import build_dsl, compile_sql
from app.data.sandbox import execute_sync
from app.llm.mock import _parse_question


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict:
    """意图 DSL 简写构造器。filters 用 (field, value) 元组，value 为 list 时编译为 IN。"""
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
BT = lambda s, e: {"mode": "between", "start": s, "end": e}

# (id, kind, question, dsl | None, role)
SPECS: list[tuple[str, str, str, dict | None, str]] = [
    # ---------- AGG 单指标聚合 ×6 ----------
    ("b-001", "query", "上个月的销售额是多少", dsl("revenue", time=L1), "boss"),
    ("b-002", "query", "上月订单量有多少", dsl("order_count", time=L1), "boss"),
    ("b-003", "query", "上月客单价是多少", dsl("avg_order_value", time=L1), "boss"),
    ("b-004", "query", "上月毛利率是多少", dsl("gross_margin_rate", time=L1), "boss"),
    ("b-005", "query", "上月消费会员数是多少", dsl("member_count", time=L1), "boss"),
    ("b-006", "query", "最近3个月销售额是多少", dsl("revenue", time=L3), "boss"),
    # ---------- BRK 维度分解 ×3 ----------
    ("b-007", "query", "各品类销售额", dsl("revenue", dims=["category"]), "boss"),
    ("b-008", "query", "最近3个月各渠道销售额", dsl("revenue", dims=["channel"], time=L3), "boss"),
    ("b-009", "query", "各细分品类毛利率", dsl("gross_margin_rate", dims=["subcategory"]), "boss"),
    # ---------- TOP 排名 ×2 ----------
    ("b-010", "query", "销售额最高的门店", dsl("revenue", dims=["store"]), "boss"),
    ("b-011", "query", "客单价最低的店型", dsl("avg_order_value", dims=["store_type"], direction="asc"), "boss"),
    # ---------- TRD 趋势 ×2 ----------
    ("b-012", "query", "近6个月各渠道销售额趋势", dsl("revenue", dims=["month", "channel"], time=L6), "boss"),
    ("b-013", "query", "近6个月饮品品类销售额趋势", dsl("revenue", dims=["month"], filters=[("category", "饮品")], time=L6), "boss"),
    # ---------- FLT 取值过滤 ×4 ----------
    ("b-014", "query", "外卖渠道上月销售额", dsl("revenue", filters=[("channel", "外卖")], time=L1), "boss"),
    ("b-015", "query", "华南地区上月毛利额", dsl("gross_margin", filters=[("region", "华南")], time=L1), "boss"),
    ("b-016", "query", "旗舰店店型最近3个月的销售额", dsl("revenue", filters=[("store_type", "旗舰店")], time=L3), "boss"),
    ("b-017", "query", "2025年1月到6月的销售额", dsl("revenue", time=BT("2025-01", "2025-06")), "boss"),
    # ---------- ROL 行级权限（B 数据集复用全局角色表） ×1 ----------
    ("b-018", "query", "上月各渠道销售额", dsl("revenue", dims=["channel"], time=L1), "south"),
    # ---------- DRK 归因/下钻 ×3 ----------
    ("b-019", "drill", "为什么上月外卖渠道销售额增长", None, "boss"),
    ("b-020", "drill", "上个月整体毛利率为什么下滑", None, "boss"),
    ("b-021", "drill", "分析上月各渠道的销售额结构", None, "boss"),
    # ---------- CLA 澄清 ×2 ----------
    ("b-022", "clarify", "卖得怎么样", None, "boss"),
    ("b-023", "clarify", "上个月卖得怎么样", None, "boss"),
    # ---------- REF 拒答 ×3 ----------
    ("b-024", "refusal", "预测下季度的销售额", None, "boss"),
    ("b-025", "refusal", "客户满意度怎么样", None, "boss"),
    ("b-026", "refusal", "写一段上月销售总结", None, "boss"),
]


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def self_check() -> list[str]:
    """构建期自检：coffee 词表的 mock 解析器重答 query 题，与金标同库（B 库）执行比对。"""
    db = COFFEE_SPEC.db_abs_path
    if not db.exists():
        from app.data.generator_b import generate_b

        print(f"B 库不存在，先按默认参数生成：{db}")
        db.parent.mkdir(parents=True, exist_ok=True)
        generate_b(db)
    problems: list[str] = []
    for sid, kind, question, spec, role in SPECS:
        if kind != "query":
            continue
        gold_sql, _ = compile_sql(build_dsl(spec, COFFEE_SPEC), role=role, spec=COFFEE_SPEC)
        try:
            parsed = build_dsl(_parse_question(question, COFFEE_SPEC), COFFEE_SPEC)
            got_sql, _ = compile_sql(parsed, role=role, spec=COFFEE_SPEC)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} [{role}] mock 解析/编译失败: {e}\n    Q: {question}")
            continue
        try:
            ok = _canon(execute_sync(got_sql, db_path=db).rows) == _canon(
                execute_sync(gold_sql, db_path=db).rows
            )
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}")
            continue
        if not ok:
            problems.append(
                f"{sid} [{role}] mock 结果与金标不一致\n    Q: {question}\n    gold: {gold_sql}\n    got : {got_sql}"
            )
    return problems


def build(path: Path | None) -> str:
    items = []
    for sid, kind, question, spec, role in SPECS:
        item: dict = {"id": sid, "kind": kind, "question": question, "role": role, "split": "main"}
        if spec is not None:
            item["gold_sql"] = compile_sql(build_dsl(spec, COFFEE_SPEC), role=role, spec=COFFEE_SPEC)[0]
        items.append(item)
    from collections import Counter

    stats = Counter(i["kind"] for i in items)
    note = (
        "数据集 B（线下连锁咖啡）评测集：金标由 compile_sql(spec=coffee) 生成"
        "（scripts/build_dataset_b.py 可重建/自检），判分执行库为 data/askbi_b.duckdb。"
        f"构成：{dict(sorted(stats.items()))}；全部 main split（B 集用于多数据源回归验证，无调参流程）。"
    )
    payload = {"note": note, "items": items}
    if path is not None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return note


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只跑自检，不写盘")
    args = ap.parse_args()

    problems = self_check()
    if problems:
        print(f"自检未通过 {len(problems)} 条：")
        for p in problems:
            print("  -", p)
        return 1
    print("自检通过：全部 query 题 mock（coffee 词表）重答与金标同库一致")

    note = build(None if args.check else ROOT / "eval" / "dataset_b.json")
    print(note)
    if not args.check:
        print("已写入 eval/dataset_b.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
