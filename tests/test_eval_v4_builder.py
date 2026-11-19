"""v4 扩容评测集构建器守门测试（[1B6] askbi 扩容车道）。

守门内容：
1. 冻结前缀完整性：dataset_v4_600.json 前 300 条与 v3_300 **逐 dict 相等**（逐字保留）+
   总数 600 + 新条目 id 连续 v4-301..v4-600 + 批次字段；
2. 新增 300 条配比断言 189/37/38/36（实读 v3 分布取整）+ split/role 分布；
3. runner --dataset 具名别名 v4/d（1B5 踩坑补遗：d 曾漏配）；
4. 同题跨源变体：10 对 ecommerce×coffee——coffee 条目带 dataset 字段且金标出自
   coffee spec（SQL 引用 tickets/ticket_lines 物理表），ecommerce 孪生引用 orders；
5. 跨源评测机制：runner.run() 对 1 条 ecommerce + 1 条 coffee 混合小集按条目切源
   判分通过且 settings.dataset 恢复原值（机制验证，仅 2 条，非跑分）；
6. v4 全部 query 金标 LIMIT 截断边界 strict 复核（DB 存在时；对齐 test_1b6_lane_b 模式）。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
V3 = "eval/dataset_v3_300.json"
V4 = "eval/dataset_v4_600.json"


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _v4_items() -> list[dict]:
    return _load(V4)["items"]


def test_v4_freeze_prefix_integrity():
    """冻结前缀：前 300 条与 v3_300 逐 dict 相等；新条目 id 连续且带本批批次字段。"""
    v3 = _load(V3)["items"]
    v4 = _v4_items()
    assert len(v4) == 600
    assert v4[: len(v3)] == v3, "冻结前缀与 dataset_v3_300.json 存在漂移"
    new = v4[len(v3):]
    assert [i["id"] for i in new] == [f"v4-{n}" for n in range(301, 601)]
    assert all(i.get("batch") == "1B6-AB-v4-expansion" for i in new)


def test_v4_new_mix():
    """新增 300 条配比 189/37/38/36；split 全 main；role 分布 south/north 各 7。"""
    v4 = _v4_items()
    new = v4[300:]
    assert len(new) == 300
    assert dict(Counter(i["kind"] for i in new)) == {
        "query": 189, "drill": 37, "clarify": 38, "refusal": 36,
    }
    assert all(i.get("split") == "main" for i in new)
    assert dict(Counter(i["role"] for i in new)) == {"boss": 286, "south": 7, "north": 7}


def test_v4_runner_aliases():
    """--dataset 具名别名：v4 → dataset_v4_600.json；d → dataset_d.json（1B5 踩坑补遗）。"""
    from app.eval.runner import DATASET_ALIASES, _resolve_dataset_arg

    assert DATASET_ALIASES["v4"] == "eval/dataset_v4_600.json"
    assert DATASET_ALIASES["d"] == "eval/dataset_d.json"
    assert _resolve_dataset_arg("v4").exists()
    assert _resolve_dataset_arg("d").exists()
    # 向后兼容：路径写法与缺省行为不变
    assert _resolve_dataset_arg("eval/dataset_b.json").exists()
    assert _resolve_dataset_arg("v1") is None


def test_v4_cross_source_pairs():
    """同题跨源 10 对：coffee 条目带 dataset 字段、金标出自 coffee spec（tickets/ticket_lines），
    ecommerce 孪生金标引用 orders——两库两套物理模型同题不同标。"""
    new = _v4_items()[300:]
    coffee = [i for i in new if i.get("dataset") == "coffee"]
    assert len(coffee) == 10
    assert all(i["kind"] == "query" for i in coffee)
    ecom = [i for i in new if "dataset" not in i]
    ecom_qs = {i["question"] for i in ecom}
    assert all(i["question"] in ecom_qs for i in coffee), "coffee 条目缺 ecommerce 孪生"
    assert all("ticket_lines" in i["gold_sql"] and "FROM orders" not in i["gold_sql"] for i in coffee)
    twins = [i for i in ecom if i["question"] in {c["question"] for c in coffee}]
    assert len(twins) == 10
    assert all("ticket_lines" not in i["gold_sql"] and "FROM orders" in i["gold_sql"] for i in twins)


def test_v4_cross_source_eval_smoke(tmp_path):
    """跨源评测机制（runner.run 按条目 dataset 切源）：1 ecommerce + 1 coffee 混合小集
    双双判分通过，且 settings.dataset 评测后恢复原值。仅 2 条，机制验证非跑分。"""
    from app.config import get_settings
    from app.eval.runner import run

    items = _v4_items()
    pick = [next(i for i in items if i["id"] == "v4-355"),
            next(i for i in items if i["id"] == "v4-356")]
    assert pick[0]["question"] == pick[1]["question"]
    tiny = tmp_path / "tiny_v4_smoke.json"
    tiny.write_text(json.dumps({"note": "smoke", "items": pick}, ensure_ascii=False), encoding="utf-8")

    saved = get_settings().dataset
    try:
        summary = asyncio.run(run("v4_smoke", "none", None, None, tiny, None))
    finally:
        get_settings().dataset = saved  # 双保险（run() 内部本就按条目恢复）
    assert summary["total"] == 2 and summary["accuracy"] == 1.0
    assert summary["by_dataset"] == {
        "ecommerce": {"n": 1, "ok": 1}, "coffee": {"n": 1, "ok": 1},
    }
    assert get_settings().dataset == saved


def test_build_script_wires_four_fold_checks():
    """v4 构建脚本必须接线四重自检（mock 重答 / clarifier 实判 / drill 路由 / LIMIT 边界）。"""
    src = (ROOT / "scripts" / "build_dataset_v4_600.py").read_text(encoding="utf-8")
    assert "_parse_question(" in src, "缺 query mock 重答自检"
    assert "_clarifier(" in src, "缺 clarify/refusal 实判自检"
    assert "route_node(" in src, "缺 drill heavy 路由断言"
    assert "limit_boundary_strict(" in src, "缺 LIMIT 截断边界 strict 断言"


def test_v4_limit_invariant_all_query_gold():
    """v4 全部 query 金标（冻结 189 + 新增 189，含 10 条 coffee 走 B 库）边界 strict 复核。"""
    db_a = ROOT / "data" / "askbi.duckdb"
    db_b = ROOT / "data" / "askbi_b.duckdb"
    if not db_a.exists() or not db_b.exists():
        pytest.skip("本地库不存在：构建期不变量由构建脚本 --check 兜底")
    from app.eval.limit_invariant import limit_boundary_strict

    violations = []
    for it in _v4_items():
        if it["kind"] != "query" or not it.get("gold_sql"):
            continue
        judge_db = db_b if it.get("dataset") == "coffee" else db_a
        ok, detail = limit_boundary_strict(it["gold_sql"], db_path=judge_db)
        if not ok:
            violations.append(f"{it['id']}: {detail}")
    assert not violations, f"LIMIT 边界并列 {len(violations)} 条: {violations}"
