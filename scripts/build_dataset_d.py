"""构建数据集 D 评测集（eval/dataset_d.json）：金标一律由 compile_sql(spec=hotel) 生成。

与 build_dataset_c.py 同款设计，目标数据集换成连锁酒店酒旅（app/data/dataset_d.py，
[1B4-AB] 任务二第四数据源）：
- query 题内嵌"意图 DSL"，gold_sql = compile_sql(意图DSL, role, spec=hotel) —— 金标与线上编译器同源；
- 构建期自检（三重）：
  1. query：mock 解析器（hotel 词表）重答每道 query 题，与金标在同一 DuckDB（D 库）
     执行比对（LIMIT 剥离口径，防并列截断边界执行间翻转），不通过即打印差异——
     措辞超出解析器覆盖/意图 DSL 写错在构建期暴露；
  2. drill：解析可产出合法 DSL 且路由为 heavy（drill 判分依赖 full 链路的叙述+QA）；
  3. clarify/refusal：mock clarifier 实判 verdict 与期望 kind 一致；
- drill/clarify/refusal 无金标（runner 按 answer kind/叙述+QA 判分）；
- 全部条目 split=main（D 集用于多数据源回归验证，无调参流程，不设 holdout）。
- 措辞全部先经 mock 探针验证解析形态再定稿（探针-修正记录见本批提交信息）。

用法（项目根，须用 askbi/.venv/Scripts/python.exe）：
    python scripts/build_dataset_d.py            # 构建 + 自检，写入 eval/dataset_d.json
    python scripts/build_dataset_d.py --check    # 只自检不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.nodes import route_node
from app.data.dataset_d import HOTEL_SPEC
from app.data.dsl import build_dsl, compile_sql
from app.data.sandbox import execute_sync
from app.llm.mock import _clarifier, _parse_question


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
    ("d-001", "query", "上个月的房费收入是多少", dsl("room_revenue", time=L1), "boss"),
    ("d-002", "query", "上月预订单数有多少", dsl("booking_count", time=L1), "boss"),
    ("d-003", "query", "上月间夜数有多少", dsl("room_nights", time=L1), "boss"),
    ("d-004", "query", "上月入住宾客数是多少", dsl("guest_count", time=L1), "boss"),
    ("d-005", "query", "上月平均房价是多少", dsl("adr", time=L1), "boss"),
    ("d-006", "query", "最近3个月房费收入是多少", dsl("room_revenue", time=L3), "boss"),
    # ---------- BRK 维度分解 ×3 ----------
    ("d-007", "query", "各酒店类型的房费收入", dsl("room_revenue", dims=["hotel_type"]), "boss"),
    ("d-008", "query", "最近3个月各渠道房费收入", dsl("room_revenue", dims=["channel"], time=L3), "boss"),
    ("d-009", "query", "各房型毛利率", dsl("gross_margin_rate", dims=["room_type"]), "boss"),
    # ---------- TOP 排名 ×2 ----------
    ("d-010", "query", "房费收入最高的城市", dsl("room_revenue", dims=["city"]), "boss"),
    ("d-011", "query", "平均房价最低的房型", dsl("adr", dims=["room_type"], direction="asc"), "boss"),
    # ---------- TRD 趋势 ×2 ----------
    ("d-012", "query", "近6个月各渠道房费收入趋势", dsl("room_revenue", dims=["month", "channel"], time=L6), "boss"),
    ("d-013", "query", "近6个月房费收入趋势", dsl("room_revenue", dims=["month"], time=L6), "boss"),
    # ---------- FLT 取值过滤 ×4 ----------
    ("d-014", "query", "旅行社渠道上月房费收入", dsl("room_revenue", filters=[("channel", "旅行社")], time=L1), "boss"),
    ("d-015", "query", "度假店型上月毛利额", dsl("gross_margin", filters=[("hotel_type", "度假")], time=L1), "boss"),
    ("d-016", "query", "大床房型最近3个月的预订单数", dsl("booking_count", filters=[("room_type", "大床")], time=L3), "boss"),
    ("d-017", "query", "2025年1月到6月的房费收入", dsl("room_revenue", time=BT("2025-01", "2025-06")), "boss"),
    # ---------- ROL 行级权限（D 数据集复用全局角色表） ×1 ----------
    ("d-018", "query", "上月各渠道房费收入", dsl("room_revenue", dims=["channel"], time=L1), "south"),
    # ---------- DRK 归因/下钻 ×3 ----------
    ("d-019", "drill", "为什么上月度假店型的房费收入增长", None, "boss"),
    ("d-020", "drill", "上个月整体毛利率为什么下滑", None, "boss"),
    ("d-021", "drill", "分析上月各渠道的房费收入结构", None, "boss"),
    # ---------- CLA 澄清 ×2 ----------
    ("d-022", "clarify", "生意怎么样", None, "boss"),
    ("d-023", "clarify", "整体表现如何", None, "boss"),
    # ---------- REF 拒答 ×3 ----------
    ("d-024", "refusal", "预测下季度的房费收入", None, "boss"),
    ("d-025", "refusal", "住客满意度怎么样", None, "boss"),
    ("d-026", "refusal", "帮我写一份上月经营总结", None, "boss"),
]


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def _strip_limit(sql: str) -> tuple[str, str]:
    """剥掉编译器恒定的尾行 LIMIT，返回 (主体 SQL, LIMIT 行)——防并列截断边界执行间翻转。"""
    body, _, limit_line = sql.rpartition("\nLIMIT")
    return body, ("LIMIT" + limit_line)


def self_check() -> list[str]:
    """构建期自检：hotel 词表的 mock 解析器重答 query 题，与金标同库（D 库）执行比对；
    drill 断言解析合法+heavy 路由；clarify/refusal 断言 clarifier verdict。"""
    db = HOTEL_SPEC.db_abs_path
    if not db.exists():
        from app.data.generator_d import generate_d

        print(f"D 库不存在，先按默认参数生成：{db}")
        db.parent.mkdir(parents=True, exist_ok=True)
        generate_d(db)
    problems: list[str] = []
    for sid, kind, question, spec, role in SPECS:
        if kind == "query":
            gold_sql, _ = compile_sql(build_dsl(spec, HOTEL_SPEC), role=role, spec=HOTEL_SPEC)
            try:
                parsed = build_dsl(_parse_question(question, HOTEL_SPEC), HOTEL_SPEC)
                got_sql, _ = compile_sql(parsed, role=role, spec=HOTEL_SPEC)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{sid} [{role}] mock 解析/编译失败: {e}\n    Q: {question}")
                continue
            gold_body, gold_limit = _strip_limit(gold_sql)
            got_body, got_limit = _strip_limit(got_sql)
            if got_limit != gold_limit:
                problems.append(f"{sid} [{role}] LIMIT 不一致: {got_limit!r} vs {gold_limit!r}\n    Q: {question}")
                continue
            try:
                ok = _canon(execute_sync(got_body, db_path=db).rows) == _canon(
                    execute_sync(gold_body, db_path=db).rows
                )
            except Exception as e:  # noqa: BLE001
                problems.append(f"{sid} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}")
                continue
            if not ok:
                problems.append(
                    f"{sid} [{role}] mock 结果与金标不一致\n    Q: {question}\n    gold: {gold_sql}\n    got : {got_sql}"
                )
        elif kind == "drill":
            try:
                build_dsl(_parse_question(question, HOTEL_SPEC), HOTEL_SPEC)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{sid} [drill] mock 解析产出非法 DSL: {e}\n    Q: {question}")
                continue
            if route_node({"question": question, "events": []})["depth"] != "full":
                problems.append(f"{sid} [drill] 路由为 light（drill 判分依赖 full 链路）: {question}")
        else:
            verdict = _clarifier({"question": question, "dataset": "hotel"})["verdict"]
            expect = "refuse" if kind == "refusal" else "clarify"
            if verdict != expect:
                problems.append(f"{sid} [{kind}] mock verdict={verdict} 期望 {expect}: {question}")
    return problems


def build(path: Path | None) -> str:
    items = []
    for sid, kind, question, spec, role in SPECS:
        item: dict = {"id": sid, "kind": kind, "question": question, "role": role, "split": "main"}
        if spec is not None:
            item["gold_sql"] = compile_sql(build_dsl(spec, HOTEL_SPEC), role=role, spec=HOTEL_SPEC)[0]
        items.append(item)
    from collections import Counter

    stats = Counter(i["kind"] for i in items)
    note = (
        "数据集 D（连锁酒店酒旅）评测集：金标由 compile_sql(spec=hotel) 生成"
        "（scripts/build_dataset_d.py 可重建/自检），判分执行库为 data/askbi_d.duckdb。"
        "新 query 题全部通过构建期 mock 重答自检（LIMIT 剥离口径），clarify/refusal 经 mock "
        "clarifier 实判、drill 经解析+heavy 路由断言。"
        f"构成：{dict(sorted(stats.items()))}；全部 main split（D 集用于多数据源回归验证，无调参流程）。"
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
    print("自检通过：全部 query 题 mock（hotel 词表）重答与金标同库一致；"
          "drill 解析合法且路由 heavy；clarify/refusal verdict 实判一致")

    note = build(None if args.check else ROOT / "eval" / "dataset_d.json")
    print(note)
    if not args.check:
        print("已写入 eval/dataset_d.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
