"""[1B6] deep 扩容车道守门：multiturn_deep_v2.jsonl（60 组）/ 冻结前缀逐字节 / ecommerce 零变化 / runner 别名 / 双挂起机制。

红线对应：
- 冻结评测集零字节：eval/multiturn_deep.jsonl 一个字节不改（md5 钉死，与
  tests/test_multiturn_cross.py FROZEN_MD5 同值双保险）；新文件 eval/multiturn_deep_v2.jsonl
  的冻结 20 组会话行与其逐字节相等（逐行 md5 口径的程序化等价断言）；
- ecommerce 默认行为零变化：deep 集定位单源——60 组金标全部含 ecommerce base_from、
  绝不含 coffee/saas/hotel 物理表；runner 缺省集与 30 条断言不动，本车道只追加一行别名；
- 结构/能力面配比/构建期自检全清由本文件程序化守住（60 组逐轮四重探针在
  scripts/build_multiturn_deep_v2.py --check 与测试 5 双入口复跑）。

DB 依赖：金标边界 strict 断言用评测库 data/askbi.duckdb（构建产物，缺失时跳过，
与 tests/test_1b6_lane_b.py 同约定）；双挂起机制测试用 conftest 测试库。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

V2 = ROOT / "eval" / "multiturn_deep_v2.jsonl"
FROZEN = ROOT / "eval" / "multiturn_deep.jsonl"

# 冻结集字节钉死（[1B6] 批次开工时快照；与 test_multiturn_cross.py 同值——双文件双保险）
FROZEN_MD5 = "153045e1276303db9178c3d95ee3bb2a"

FROZEN_GROUPS = 20
NEW_GROUPS = 40
TOTAL_GROUPS = FROZEN_GROUPS + NEW_GROUPS

# 消解说明词表（与 app/context.py 说明文案同源锚定；note_contains 只允许这些锚点）
NOTE_VOCAB = {"指代消解", "无同类型实体可替换", "上轮含多个"}
KINDS = {"answer", "clarify", "refusal", "suspend"}


def _load_builder():
    """按文件路径加载 scripts/build_multiturn_deep_v2.py（脚本导入期无副作用）。"""
    path = ROOT / "scripts" / "build_multiturn_deep_v2.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _v2_lines() -> list[str]:
    return [ln for ln in V2.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _v2_groups() -> list[dict]:
    return [json.loads(ln) for ln in _v2_lines()[1:]]


# ---- 1. 新文件结构：60 组 / id 接续 / 能力面配比 / 轮数边界 / 字段纪律 ----

def test_v2_file_structure():
    mod = _load_builder()
    lines = _v2_lines()
    assert len(lines) == TOTAL_GROUPS + 1, f"应为 1 元信息行 + {TOTAL_GROUPS} 组会话行，实际 {len(lines)} 行"
    note = json.loads(lines[0])
    assert "_note" in note and "md21..md60" in note["_note"] and "逐行字节保留" in note["_note"], \
        "v2 元信息行须声明冻结前缀与新增 id 段"

    groups = _v2_groups()
    assert [g["id"] for g in groups] == [f"md{i:02d}" for i in range(1, TOTAL_GROUPS + 1)], \
        "会话行 id 应为 md01..md60 连续"

    # 冻结段：md01..md20 无 batch 字段；新增段：md21..md60 batch=deep_v2
    for g in groups[:FROZEN_GROUPS]:
        assert "batch" not in g, f"{g['id']} 冻结组不得带 batch 字段"
    for g in groups[FROZEN_GROUPS:]:
        assert g.get("batch") == mod.BATCH, f"{g['id']} 新增组缺 batch={mod.BATCH}"

    # 能力面配比（单源 deep_v2 新增 40 组九类场景）
    scen_stat: dict[str, int] = {}
    for g in groups[FROZEN_GROUPS:]:
        scen_stat[g["scenario"]] = scen_stat.get(g["scenario"], 0) + 1
    assert scen_stat == mod.AREA_PLAN, f"能力面配比不符：{scen_stat} vs {mod.AREA_PLAN}"

    # 逐组：轮数边界 / 轮型合法 / 金标齐备 / suspend 标记与轮型互证 / note 词表锚定 / turn 级 role 收口
    # （轮数边界按段取口径：冻结段沿用 deep builder 的 4-6，新增段用 v2 场景边界——冻结 md20
    #   是 mixed_long 6 轮，早于 7-8 加长规则，逐字节保留不适用新边界）
    for g in groups:
        if "batch" in g:
            lo, hi = mod.TURN_BOUNDS.get(g["scenario"], mod.TURN_BOUNDS["_default"])
        else:
            lo, hi = 4, 6
        assert lo <= len(g["turns"]) <= hi, f"{g['id']} 轮数 {len(g['turns'])} 越界（{lo}-{hi}）"
        has_suspend_turn = False
        for t in g["turns"]:
            exp = t["expect"]
            assert exp["kind"] in KINDS, f"{g['id']} 未知轮型 {exp['kind']}"
            if exp["kind"] == "answer":
                assert exp.get("gold_sql") and exp["gold_sql"].lstrip().startswith("SELECT"), \
                    f"{g['id']} answer 轮缺金标"
            if "note_contains" in exp:
                assert exp["note_contains"] in NOTE_VOCAB, \
                    f"{g['id']} note_contains 超出锚定词表: {exp['note_contains']}"
            has_suspend_turn |= exp["kind"] == "suspend"
        assert bool(g.get("suspend")) == has_suspend_turn, \
            f"{g['id']} suspend 标记与 suspend 轮型不一致"
    # turn 级 role 覆盖只允许出现在缓存边界组（当前唯一使用者 md33）
    for g in groups:
        for t in g["turns"]:
            if "role" in t:
                assert g["scenario"] == "cache_boundary" and g["id"] == "md33", \
                    f"{g['id']} 出现计划外 turn 级 role 覆盖"


# ---- 2. 冻结前缀逐字节保留 + 冻结源零字节 ----

def test_frozen_prefix_byte_identical():
    frozen_lines = [ln for ln in FROZEN.read_text(encoding="utf-8").splitlines() if ln.strip()]
    v2_lines = _v2_lines()
    assert hashlib.md5(FROZEN.read_bytes()).hexdigest() == FROZEN_MD5, \
        "冻结集 multiturn_deep.jsonl 被改动（红线：冻结评测集零字节）"
    assert v2_lines[1:1 + FROZEN_GROUPS] == frozen_lines[1:], \
        "v2 冻结前缀 20 组会话行与冻结源逐字节不等（红线：冻结逐行保留）"
    assert len(frozen_lines) == FROZEN_GROUPS + 1


# ---- 3. ecommerce 单源归属：金标绝不含跨源物理表 ----

def test_gold_sql_ecommerce_only():
    foreign = ("ticket_lines", "invoice_lines", "stay_lines")   # coffee/saas/hotel base_from 表
    for g in _v2_groups():
        for tno, t in enumerate(g["turns"], 1):
            sql = (t.get("expect") or {}).get("gold_sql")
            if not sql:
                continue
            assert "FROM orders o JOIN order_items" in sql, f"{g['id']}.t{tno} 金标缺 ecommerce base_from"
            for f in foreign:
                assert f not in sql, f"{g['id']}.t{tno} 金标混入他源表 {f}"


# ---- 4. 金标截断边界 strict（60 组全部 answer 轮，评测库；缺库跳过） ----

def test_gold_boundary_strict_on_eval_db():
    from app.eval.limit_invariant import limit_boundary_strict

    db = ROOT / "data" / "askbi.duckdb"
    if not db.exists():
        pytest.skip("评测库不存在（data/askbi.duckdb）：构建脚本 --check 兜底")
    violations = []
    for g in _v2_groups():
        for tno, t in enumerate(g["turns"], 1):
            sql = (t.get("expect") or {}).get("gold_sql")
            if not sql:
                continue
            ok, detail = limit_boundary_strict(sql, db_path=db)
            if not ok:
                violations.append(f"{g['id']}.t{tno}: {detail}")
    assert not violations, f"LIMIT 边界并列（截断集合非确定）{len(violations)} 条: {violations}"


# ---- 5. 构建期自检复跑全清（60 组逐轮四重探针 + 完整性校验） ----

def test_v2_builder_self_check_and_integrity_clean(db_path):
    mod = _load_builder()
    problems = mod.self_check() + mod.integrity_check()
    assert not problems, f"deep_v2 自检 {len(problems)} 条: {problems}"


# ---- 6. runner 接线：deep_v2 别名追加且历史口径零变化 ----

def test_runner_alias_and_default_unchanged():
    src = (ROOT / "scripts" / "run_multiturn_eval.py").read_text(encoding="utf-8")
    assert '"multiturn_deep_v2": "eval/multiturn_deep_v2.jsonl"' in src, \
        "run_multiturn_eval.DATASET_ALIASES 缺 multiturn_deep_v2 别名"
    assert '"multiturn_deep": "eval/multiturn_deep.jsonl"' in src and '"followup"' in src, \
        "历史别名（multiturn_deep/followup）不得被本车道改动"
    assert 'DATASET = ROOT / "eval" / "dataset_v2_followup.json"' in src and "len(items) != 30" in src, \
        "缺省评测集与 30 条断言必须保持历史口径"
    assert "judge_db = get_spec(settings.dataset).db_abs_path" in src, \
        "判分库接线（跨源车道装置）不得被本车道改动"


# ---- 7. 双挂起机制：同 thread 两次 interrupt + 两次恢复均出数（md28 链路的图级验证） ----

@pytest.mark.asyncio
async def test_double_suspend_on_same_thread(db_path):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from app.agents.graph import build_graph

    async def turn(graph, cfg, payload):
        suspended = None
        last: dict = {}
        # langgraph 会原地改写 config dict——每次 astream 新建（tests/test_clarify_suspend 踩坑）
        async for chunk in graph.astream(payload, config=dict(cfg), stream_mode="updates"):
            if "__interrupt__" in chunk:
                suspended = chunk["__interrupt__"][0].value
            last.update(chunk)
        answer = (last.get("finalizer") or {}).get("answer") or {}
        return suspended, answer.get("kind"), (last.get("clarify_suspend") or {}).get("question", "")

    graph = build_graph(clarify_suspend=True, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "eval-deep-v2-md28"}}

    def new_input(q: str) -> dict:
        # 与 runner run_turn 同构：非 resume 轮 payload 显式带 depth=""（[1B4-AB] 修复口径）
        return {"question": q, "role": "boss", "depth": "", "events": [], "usage": {}}

    s1, k1, _ = await turn(graph, cfg, new_input("最近卖得怎么样"))
    assert s1 and "question" in s1 and k1 is None, "第一次澄清未挂起"
    s2, k2, m2 = await turn(graph, cfg, Command(resume="销售额"))
    assert not s2 and k2 == "answer" and "用户澄清" in m2, "第一次恢复未出数或未合并澄清"
    s3, k3, _ = await turn(graph, cfg, new_input("上月各品类销售额"))
    assert not s3 and k3 == "answer", "挂起 thread 上的新完整问题应直接出数"
    s4, k4, _ = await turn(graph, cfg, new_input("整体表现如何"))
    assert s4 and "question" in s4 and k4 is None, "同 thread 第二次澄清未再挂起"
    s5, k5, m5 = await turn(graph, cfg, Command(resume="毛利率"))
    assert not s5 and k5 == "answer" and "用户澄清" in m5, "第二次恢复未出数或未合并澄清"
