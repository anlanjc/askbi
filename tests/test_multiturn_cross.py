"""[1B6] 跨源多轮评测守门：三源结构 / 金标 spec 归属 / 冻结集零干扰 / runner 接线 ecommerce 零变化。

红线对应：
- 冻结评测集零字节：multiturn_deep.jsonl 与 dataset_v2_followup.json md5 钉死；
- ecommerce 默认多轮行为零变化：runner 判分库接线在默认数据集下是恒等映射
  （get_spec("ecommerce").db_abs_path == settings.db_abs_path），judge_query 判定结果
  传/不传 db_path 完全一致；multiturn_deep 95 轮构建期自检复跑仍全清；
- 跨源三文件（multiturn_cross_b/_c/_d.json）结构、场景模板覆盖、金标物理归属
  （base_from 表名随 spec）、构建期自检全清由本文件程序化守住。

DB 依赖：coffee/saas/hotel spec 库（data/askbi_b/c/d.duckdb）为本地构建产物，
缺失时跳过（构建脚本 --check 兜底，与 tests/test_1b6_lane_b.py 同约定）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

CROSS_FILES = {
    "coffee": ("eval/multiturn_cross_b.json", "data/askbi_b.duckdb", "ticket_lines"),
    "saas": ("eval/multiturn_cross_c.json", "data/askbi_c.duckdb", "invoice_lines"),
    "hotel": ("eval/multiturn_cross_d.json", "data/askbi_d.duckdb", "stay_lines"),
}

SCENARIO_TEMPLATES = {
    "resolve_chain", "constraint_keep", "clarify_then_drill", "topic_switch",
    "topic_switch_cache", "role_mixed", "refusal_mixed", "clarify_mixed", "mixed_long",
}

# 冻结集字节钉死（[1B6] 批次开工时快照）：多轮 deep 95 轮与 followup 30 条语义零变化的前提
FROZEN_MD5 = {
    "eval/multiturn_deep.jsonl": "153045e1276303db9178c3d95ee3bb2a",
    "eval/dataset_v2_followup.json": "6c21a018266a642571dae208de96fbcf",
}


def _load_script(rel: str):
    """按文件路径加载 scripts/ 下脚本模块（scripts 非包；脚本导入期无副作用，main 受 guard）。"""
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cross_items(rel: str) -> list[dict]:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))["items"]


def _turns_with_gold():
    for source, (rel, _db, _table) in CROSS_FILES.items():
        for it in _cross_items(rel):
            for tno, t in enumerate(it["turns"], 1):
                if (t.get("expect") or {}).get("gold_sql"):
                    yield source, it["id"], tno, t["expect"]["gold_sql"]


# ---- 1. 三文件结构（组数/轮数/条目形状） ----

def test_cross_files_structure():
    total_groups = total_turns = 0
    scenarios: set[str] = set()
    for source, (rel, _db, _table) in CROSS_FILES.items():
        items = _cross_items(rel)
        assert 6 <= len(items) <= 8, f"{source} 组数 {len(items)} 越界（要求 6-8）"
        total_groups += len(items)
        for it in items:
            assert it["id"] and it["scenario"] in SCENARIO_TEMPLATES and it.get("role")
            assert 3 <= len(it["turns"]) <= 5, f"{it['id']} 轮数越界（要求 3-5）"
            for t in it["turns"]:
                assert t.get("q")
                exp = t["expect"]
                assert exp["kind"] in ("answer", "clarify", "refusal", "suspend")
                if exp["kind"] == "answer":
                    assert exp.get("gold_sql"), f"{it['id']} answer 轮缺金标"
        scenarios |= {i["scenario"] for i in items}
        total_turns += sum(len(i["turns"]) for i in items)
    assert 18 <= total_groups <= 24, f"合计组数 {total_groups} 越界"
    assert 70 <= total_turns <= 110, f"合计轮数 {total_turns} 越界"
    assert scenarios == SCENARIO_TEMPLATES, f"scenario 模板覆盖缺失：{SCENARIO_TEMPLATES - scenarios}"


def test_cross_files_scenario_and_role_distribution():
    """三源并集覆盖 9 类模板 + 至少一个非 boss 角色组（ROLE_SCOPES 三源共享的金标覆盖）。"""
    scenarios: set[str] = set()
    non_boss = 0
    for rel, _db, _table in CROSS_FILES.values():
        for it in _cross_items(rel):
            scenarios.add(it["scenario"])
            if it.get("role", "boss") != "boss":
                non_boss += 1
    assert scenarios == SCENARIO_TEMPLATES
    assert non_boss >= 1


# ---- 2. 金标 spec 归属（物理表随 spec，跨源绝不串库） ----

def test_gold_sql_spec_attribution():
    """每个文件的 answer 金标必须编译自对应 spec：SQL 含本源 base_from 表、不含他源表。"""
    for source, (rel, _db, table) in CROSS_FILES.items():
        others = {t for s, (_r, _d, t) in CROSS_FILES.items() if s != source}
        for _sid, _tno, sql in _gold_of(rel):
            assert "FROM" in sql and table in sql, f"{source} 金标缺本源表 {table}"
            for o in others:
                assert o not in sql, f"{source} 金标混入他源表 {o}"


def _gold_of(rel: str):
    for it in _cross_items(rel):
        for tno, t in enumerate(it["turns"], 1):
            if (t.get("expect") or {}).get("gold_sql"):
                yield it["id"], tno, t["expect"]["gold_sql"]


# ---- 3. 金标可在 spec 库执行 + 截断边界 strict（本地库缺失则跳过） ----

def test_gold_sql_executable_and_boundary_strict_on_spec_db():
    from app.data.registry import get_spec
    from app.eval.limit_invariant import limit_boundary_strict

    for source, (rel, db, _table) in CROSS_FILES.items():
        spec = get_spec(source)   # 显式取 spec：触发内置注册表装载（spec_for_db 反查依赖它）
        db_path = spec.db_abs_path
        assert db_path == ROOT / db, f"{source} spec 库路径与约定不符: {db_path}"
        if not db_path.exists():
            pytest.skip(f"本地库不存在（{db}）：由构建脚本 --check 兜底")
        for sid, tno, sql in _gold_of(rel):
            ok, detail = limit_boundary_strict(sql, db_path=db_path)
            assert ok, f"{sid}.t{tno} [{source}] {detail}"


# ---- 4. 构建期自检复跑全清（逐轮模拟 runner 接线的四重探针，spec 化口径） ----

def test_cross_builder_self_check_clean():
    mod = _load_script("scripts/build_multiturn_cross_source.py")
    for source, (_rel, db, _table) in CROSS_FILES.items():
        if not (ROOT / db).exists():
            pytest.skip(f"本地库不存在（{db}）")
        spec, items = next((s, it) for n, s, it, _t in mod.SOURCES if n == source)
        problems = mod.self_check_source(spec, items)
        assert not problems, f"[{source}] 自检 {len(problems)} 条: {problems}"


# ---- 5. runner 接线：判分库随 settings.dataset 走 + --dataset 具名别名 ----

def test_runner_judge_db_wiring():
    src = (ROOT / "scripts" / "run_multiturn_eval.py").read_text(encoding="utf-8")
    assert "judge_db = get_spec(settings.dataset).db_abs_path" in src, \
        "多轮 runner 判分库未随 settings.dataset 接线（DATASET=coffee 时金标会跑到电商库）"
    assert 'judge_query(answer, exp["gold_sql"], db_path=judge_db)' in src, \
        "judge_query 未传 db_path（金标恒在默认库执行）"
    assert '"multiturn_deep"' in src and '"followup"' in src, \
        "--dataset 具名别名（multiturn_deep/followup）缺失"


def test_runner_default_dataset_path_unchanged():
    """缺省评测集与 30 条断言保持历史口径（ecommerce 默认多轮行为零变化的入口面）。"""
    src = (ROOT / "scripts" / "run_multiturn_eval.py").read_text(encoding="utf-8")
    assert 'DATASET = ROOT / "eval" / "dataset_v2_followup.json"' in src
    assert "len(items) != 30" in src


# ---- 6. ecommerce 默认口径零变化（判分库接线是恒等映射） ----

def test_ecommerce_judge_db_is_identity_mapping(db_path):
    """默认数据集下 get_spec("ecommerce").db_abs_path == settings.db_abs_path，
    judge_query 传/不传 db_path 判定逐字一致（修前 vs 修后口径）。"""
    from app.config import get_settings
    from app.data.registry import get_spec
    from app.eval.runner import judge_query

    s = get_settings()
    judge_db = get_spec(s.dataset).db_abs_path
    assert s.dataset == "ecommerce"
    assert judge_db == s.db_abs_path

    gold_sql = "SELECT 'a' AS region, 1.0 AS value\nORDER BY value DESC\nLIMIT 100"
    res = __import__("app.data.sandbox", fromlist=["execute_sync"]).execute_sync(
        gold_sql, db_path=db_path)
    answer = {"kind": "answer", "table": {"rows": res.rows, "columns": res.columns}}
    assert judge_query(answer, gold_sql) == judge_query(answer, gold_sql, db_path=judge_db) == (True, "")


# ---- 7. 冻结集零字节（md5 钉死） ----

@pytest.mark.parametrize("rel, expected", sorted(FROZEN_MD5.items()))
def test_frozen_multiturn_files_byte_identical(rel, expected):
    got = hashlib.md5((ROOT / rel).read_bytes()).hexdigest()
    assert got == expected, f"冻结集 {rel} 被改动（红线：冻结评测集零字节）"


# ---- 8. multiturn_deep 95 轮构建期自检复跑仍全清（ecommerce 语义零变化） ----

def test_deep_builder_self_check_still_clean(db_path):
    if not db_path.exists():
        pytest.skip("默认测试库缺失")
    mod = _load_script("scripts/build_multiturn_deep.py")
    problems = mod.self_check()
    assert not problems, f"multiturn_deep 自检 {len(problems)} 条: {problems}"


# ---- 9. ROLE_SCOPES 三源共享：south 角色在三源 spec 下编译均注入华南 scope ----

def test_role_scopes_shared_across_specs():
    from app.data.dataset_b import COFFEE_SPEC
    from app.data.dataset_c import SAAS_SPEC
    from app.data.dataset_d import HOTEL_SPEC
    from app.data.dsl import ROLE_SCOPES, build_dsl, compile_sql

    assert set(ROLE_SCOPES) == {"boss", "south", "north"}
    d = {"intent": "aggregate", "metric": None, "dimensions": [], "filters": [],
         "time": {"mode": "all"}, "order_by": None, "limit": None}
    for spec, metric in ((COFFEE_SPEC, "revenue"), (SAAS_SPEC, "mrr"), (HOTEL_SPEC, "room_revenue")):
        d["metric"] = metric
        sql, caliber = compile_sql(build_dsl(d, spec=spec), role="south", spec=spec)
        assert "IN ('华南')" in sql, f"{spec.name} south 编译缺 region scope"
        assert "角色 south" in caliber
