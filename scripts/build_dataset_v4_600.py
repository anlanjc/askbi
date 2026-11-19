"""构建扩容评测集（eval/dataset_v4_600.json，[1B6] 任务）：冻结 v3_300 300 条逐字保留 + 新增 300 条。

纪律（对照 [1B4-AB] 样板 scripts/build_dataset_v3_300.py 同款，任务卡 [1B6] askbi 车道）：
- 冻结的 eval/dataset_v3_300.json 一个字节不改——本脚本只读它；构建产物内冻结条目
  与源文件**逐 dict 相等**（程序化校验，任何漂移立即报错），总数断言 600 条；
- 新条目金标一律 compile_sql 生成，禁止手写 SQL；跨源（coffee）条目走
  compile_sql(spec=COFFEE_SPEC) 路径（build_dsl(spec_dict, COFFEE_SPEC) 校验）；
- 构建期自检（四重）：
  1. query：mock 解析器重答每道新 query 题，与金标同库执行比对（LIMIT 剥离口径）；
  2. clarify/refusal：mock clarifier 实判 verdict 与期望 kind 一致（dataset 随条目走）；
  3. drill：mock 解析可产出合法 DSL（build_dsl 不抛）且路由为 heavy（drill 判分
     依赖 full 链路的叙述+QA，light 链路必挂——措辞必须含重词）；
  4. query 金标 LIMIT 截断边界 strict 断言（app/eval/limit_invariant.py，AB-29）：
     冻结 189 条（gold_sql 逐字取自冻结源文件）+ 新增 189 条逐条 fail-fast。
- 新题与冻结 300 题问题串零重复 + 新题集合内部同数据源零重复（跨源同题对
  ——"同业务问题在 ecommerce 与 coffee 各一条"——是本批显式维度，按对断言）；
- 新条目带 batch 字段标注批次来源；coffee 条目额外带 dataset 字段（runner 评测期
  按条目切源：判分库与生成链路都随 spec 走，见 app/eval/runner.py）。

新增 300 条分类配比（实读 v3_300 现状分布取整）：v3 现状 query 189（63%）/
drill 37（12.3%）/ clarify 38（12.7%）/ refusal 36（12%）→ 新增 189/37/38/36。
新维度空间（v3 未覆盖的组合）：双过滤 AND 组合 / role 变体金标（south/north 的
scope 金标随角色编译）/ 显式年月区间×分组 / 同题跨源变体（ecommerce 与 coffee
各一条）/ TopN 变体（limit 3/5/10）。
措辞全部先经 mock 探针验证解析形态再定稿（探针-修正迭代记录见本批提交信息）。

用法（项目根，须用 askbi/.venv/Scripts/python.exe）：
    python scripts/build_dataset_v4_600.py            # 构建 + 自检，写入 eval/dataset_v4_600.json
    python scripts/build_dataset_v4_600.py --check    # 只自检 + 完整性校验，不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.nodes import route_node
from app.data.dataset_b import COFFEE_SPEC
from app.data.dsl import build_dsl, compile_sql
from app.data.sandbox import execute_sync
from app.eval.limit_invariant import limit_boundary_strict
from app.llm.mock import _clarifier, _parse_question

FROZEN = ROOT / "eval" / "dataset_v3_300.json"
TARGET = ROOT / "eval" / "dataset_v4_600.json"


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict:
    """意图 DSL 简写构造器（与 build_dataset_v3_300.py 同形）。

    filters 顺序必须与 mock 解析器 filter_keywords 遍历序一致（region → category
    → channel → member_level），dims 顺序必须与 dim_keywords 遍历序一致
    （category → subcategory → region → province → city → channel → member_level），
    否则 mock 重答 SQL 与金标逐字不同（[1B4] 探针教训）。
    """
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
L24 = {"mode": "last_n_months", "n": 24}
BT = lambda s, e: {"mode": "between", "start": s, "end": e}

# 新增条目：(id, kind, question, dsl | None, role, split, dataset)
# dataset：None = 默认电商源；"coffee" = 跨源变体（金标 spec=COFFEE_SPEC，评测期按条目切源）。
# 措辞全部先经 mock 探针验证解析形态再定稿；构建期自检兜底任何回归。
# 配比按 v3_300 现状分布取整：query 189 / drill 37 / clarify 38 / refusal 36。
NEW_SPECS: list[tuple[str, str, str, dict | None, str, str, str | None]] = [
    # ---- query ×189 ----
    # A. 双过滤 AND 组合 ×26（两个过滤条件取自不同维度字段，v3 未覆盖）×22 纯双过滤
    ("v4-301", "query", "上月华北地区直播渠道的毛利额",
     dsl("gross_margin", filters=[("region", "华北"), ("channel", "直播")], time=L1), "boss", "main", None),
    ("v4-302", "query", "近3个月华东地区食品品类的销量",
     dsl("item_quantity", filters=[("region", "华东"), ("category", "食品")], time=L3), "boss", "main", None),
    ("v4-303", "query", "上月华南地区黑卡会员的净销售额",
     dsl("net_revenue", filters=[("region", "华南"), ("member_level", "黑卡")], time=L1), "boss", "main", None),
    ("v4-304", "query", "上月家居品类官网渠道的净销售额",
     dsl("net_revenue", filters=[("category", "家居"), ("channel", "官网")], time=L1), "boss", "main", None),
    ("v4-305", "query", "近6个月美妆品类银卡会员的客单价",
     dsl("avg_order_value", filters=[("category", "美妆"), ("member_level", "银卡")], time=L6), "boss", "main", None),
    ("v4-306", "query", "上月搜索渠道金卡会员的订单量",
     dsl("order_count", filters=[("channel", "搜索"), ("member_level", "金卡")], time=L1), "boss", "main", None),
    ("v4-307", "query", "近12个月西南地区社交渠道的销售额",
     dsl("revenue", filters=[("region", "西南"), ("channel", "社交")], time=L12), "boss", "main", None),
    ("v4-308", "query", "上月东北地区家居品类的毛利额",
     dsl("gross_margin", filters=[("region", "东北"), ("category", "家居")], time=L1), "boss", "main", None),
    ("v4-309", "query", "近3个月官网渠道普通会员的购买客户数",
     dsl("customer_count", filters=[("channel", "官网"), ("member_level", "普通")], time=L3), "boss", "main", None),
    ("v4-310", "query", "上月食品品类直播渠道的销量",
     dsl("item_quantity", filters=[("category", "食品"), ("channel", "直播")], time=L1), "boss", "main", None),
    ("v4-311", "query", "近6个月西北地区运动品类的订单量",
     dsl("order_count", filters=[("region", "西北"), ("category", "运动")], time=L6), "boss", "main", None),
    ("v4-312", "query", "上月华北地区银卡会员的购买客户数",
     dsl("customer_count", filters=[("region", "华北"), ("member_level", "银卡")], time=L1), "boss", "main", None),
    ("v4-313", "query", "近12个月服饰品类搜索渠道的销售额",
     dsl("revenue", filters=[("category", "服饰"), ("channel", "搜索")], time=L12), "boss", "main", None),
    ("v4-314", "query", "上月直播渠道黑卡会员的毛利率",
     dsl("gross_margin_rate", filters=[("channel", "直播"), ("member_level", "黑卡")], time=L1), "boss", "main", None),
    ("v4-315", "query", "近6个月华中地区普通会员的订单量",
     dsl("order_count", filters=[("region", "华中"), ("member_level", "普通")], time=L6), "boss", "main", None),
    ("v4-316", "query", "上月数码品类推荐渠道的毛利额",
     dsl("gross_margin", filters=[("category", "数码"), ("channel", "推荐")], time=L1), "boss", "main", None),
    ("v4-317", "query", "近3个月华东地区银卡会员的客单价",
     dsl("avg_order_value", filters=[("region", "华东"), ("member_level", "银卡")], time=L3), "boss", "main", None),
    ("v4-318", "query", "上月运动品类社交渠道的净销售额",
     dsl("net_revenue", filters=[("category", "运动"), ("channel", "社交")], time=L1), "boss", "main", None),
    ("v4-319", "query", "近12个月官网渠道金卡会员的购买客户数",
     dsl("customer_count", filters=[("channel", "官网"), ("member_level", "金卡")], time=L12), "boss", "main", None),
    ("v4-320", "query", "上月华北地区食品品类的退货率",
     dsl("refund_rate", filters=[("region", "华北"), ("category", "食品")], time=L1, direction="asc"), "boss", "main", None),
    ("v4-321", "query", "近6个月美妆品类官网渠道的订单量",
     dsl("order_count", filters=[("category", "美妆"), ("channel", "官网")], time=L6), "boss", "main", None),
    ("v4-322", "query", "上月金卡会员直播渠道的净销售额",
     dsl("net_revenue", filters=[("channel", "直播"), ("member_level", "金卡")], time=L1), "boss", "main", None),
    # A 续：双过滤 × 分组 ×4
    ("v4-323", "query", "上月华北地区直播渠道各品类的销售额",
     dsl("revenue", dims=["category"], filters=[("region", "华北"), ("channel", "直播")], time=L1), "boss", "main", None),
    ("v4-324", "query", "近3个月华东地区金卡会员各渠道的客单价",
     dsl("avg_order_value", dims=["channel"], filters=[("region", "华东"), ("member_level", "金卡")], time=L3), "boss", "main", None),
    ("v4-325", "query", "上月食品品类官网渠道各地区的净销售额",
     dsl("net_revenue", dims=["region"], filters=[("category", "食品"), ("channel", "官网")], time=L1), "boss", "main", None),
    ("v4-326", "query", "近6个月运动品类银卡会员各渠道的销量",
     dsl("item_quantity", dims=["channel"], filters=[("category", "运动"), ("member_level", "银卡")], time=L6), "boss", "main", None),
    # B. role 变体金标 ×14（south/north 的 scope 随角色编译进金标——v3 的 role 条目
    #    均为单值查询，本批补齐 role × 分组/过滤组合）south ×7
    ("v4-327", "query", "近6个月家居品类的毛利额",
     dsl("gross_margin", filters=[("category", "家居")], time=L6), "south", "main", None),
    ("v4-328", "query", "上月各品类的净销售额",
     dsl("net_revenue", dims=["category"], time=L1), "south", "main", None),
    ("v4-329", "query", "近3个月食品品类的订单量",
     dsl("order_count", filters=[("category", "食品")], time=L3), "south", "main", None),
    ("v4-330", "query", "上月各渠道的客单价",
     dsl("avg_order_value", dims=["channel"], time=L1), "south", "main", None),
    ("v4-331", "query", "近12个月美妆品类的销售额",
     dsl("revenue", filters=[("category", "美妆")], time=L12), "south", "main", None),
    ("v4-332", "query", "上月各会员等级的购买客户数",
     dsl("customer_count", dims=["member_level"], time=L1), "south", "main", None),
    ("v4-333", "query", "上月华南地区各渠道的客单价",
     dsl("avg_order_value", dims=["channel"], filters=[("region", "华南")], time=L1), "south", "main", None),
    # B 续：north ×7（scope = 华北+东北）
    ("v4-334", "query", "近3个月家居品类的净销售额",
     dsl("net_revenue", filters=[("category", "家居")], time=L3), "north", "main", None),
    ("v4-335", "query", "上月各渠道的订单量",
     dsl("order_count", dims=["channel"], time=L1), "north", "main", None),
    ("v4-336", "query", "近6个月食品品类的毛利率",
     dsl("gross_margin_rate", filters=[("category", "食品")], time=L6), "north", "main", None),
    ("v4-337", "query", "上月各品类的毛利额",
     dsl("gross_margin", dims=["category"], time=L1), "north", "main", None),
    ("v4-338", "query", "近12个月运动品类的销量",
     dsl("item_quantity", filters=[("category", "运动")], time=L12), "north", "main", None),
    ("v4-339", "query", "上月各会员等级的销售额",
     dsl("revenue", dims=["member_level"], time=L1), "north", "main", None),
    ("v4-340", "query", "近6个月华北地区各品类的销量",
     dsl("item_quantity", dims=["category"], filters=[("region", "华北")], time=L6), "north", "main", None),
    # C. 显式年月区间×分组 ×14（between 起止 + 分组维度，v3 仅 4 条零散覆盖）
    ("v4-341", "query", "2024年11月到2025年4月各渠道的毛利额",
     dsl("gross_margin", dims=["channel"], time=BT("2024-11", "2025-04")), "boss", "main", None),
    ("v4-342", "query", "2025年2月到2025年7月各品类的销量",
     dsl("item_quantity", dims=["category"], time=BT("2025-02", "2025-07")), "boss", "main", None),
    ("v4-343", "query", "2025年5月到2025年11月各会员等级的净销售额",
     dsl("net_revenue", dims=["member_level"], time=BT("2025-05", "2025-11")), "boss", "main", None),
    ("v4-344", "query", "2024年12月到2025年5月各区域的客单价",
     dsl("avg_order_value", dims=["region"], time=BT("2024-12", "2025-05")), "boss", "main", None),
    ("v4-345", "query", "2025年8月到2026年3月各城市的订单量",
     dsl("order_count", dims=["city"], time=BT("2025-08", "2026-03")), "boss", "main", None),
    ("v4-346", "query", "2025年1月到2025年9月各细分品类的毛利额",
     dsl("gross_margin", dims=["subcategory"], time=BT("2025-01", "2025-09")), "boss", "main", None),
    ("v4-347", "query", "2026年1月到2026年6月各渠道的销量",
     dsl("item_quantity", dims=["channel"], time=BT("2026-01", "2026-06")), "boss", "main", None),
    ("v4-348", "query", "2025年4月到2025年10月各品类的购买客户数",
     dsl("customer_count", dims=["category"], time=BT("2025-04", "2025-10")), "boss", "main", None),
    ("v4-349", "query", "2024年10月到2025年3月各区域的订单量",
     dsl("order_count", dims=["region"], time=BT("2024-10", "2025-03")), "boss", "main", None),
    ("v4-350", "query", "2025年6月到2026年1月各会员等级的毛利额",
     dsl("gross_margin", dims=["member_level"], time=BT("2025-06", "2026-01")), "boss", "main", None),
    ("v4-351", "query", "2025年9月到2026年4月各城市的销售额",
     dsl("revenue", dims=["city"], time=BT("2025-09", "2026-04")), "boss", "main", None),
    ("v4-352", "query", "2025年3月到2025年8月华东地区各渠道的净销售额",
     dsl("net_revenue", dims=["channel"], filters=[("region", "华东")], time=BT("2025-03", "2025-08")), "boss", "main", None),
    ("v4-353", "query", "2024年11月到2025年6月美妆品类各渠道的销量",
     dsl("item_quantity", dims=["channel"], filters=[("category", "美妆")], time=BT("2024-11", "2025-06")), "boss", "main", None),
    ("v4-354", "query", "2025年7月到2026年2月银卡会员各品类的客单价",
     dsl("avg_order_value", dims=["category"], filters=[("member_level", "银卡")], time=BT("2025-07", "2026-02")), "boss", "main", None),
    # D. 同题跨源变体 ×10 对（同业务问题在 ecommerce 与 coffee 各一条；
    #    coffee 金标走 compile_sql(spec=COFFEE_SPEC)，评测期 runner 按条目 dataset 切源）
    ("v4-355", "query", "上月各渠道的销售额",
     dsl("revenue", dims=["channel"], time=L1), "boss", "main", None),
    ("v4-356", "query", "上月各渠道的销售额",
     dsl("revenue", dims=["channel"], time=L1), "boss", "main", "coffee"),
    ("v4-357", "query", "近3个月各城市的订单量",
     dsl("order_count", dims=["city"], time=L3), "boss", "main", None),
    ("v4-358", "query", "近3个月各城市的订单量",
     dsl("order_count", dims=["city"], time=L3), "boss", "main", "coffee"),
    ("v4-359", "query", "上月华东地区的毛利额",
     dsl("gross_margin", filters=[("region", "华东")], time=L1), "boss", "main", None),
    ("v4-360", "query", "上月华东地区的毛利额",
     dsl("gross_margin", filters=[("region", "华东")], time=L1), "boss", "main", "coffee"),
    ("v4-361", "query", "2025年1月到2025年6月的订单量",
     dsl("order_count", time=BT("2025-01", "2025-06")), "boss", "main", None),
    ("v4-362", "query", "2025年1月到2025年6月的订单量",
     dsl("order_count", time=BT("2025-01", "2025-06")), "boss", "main", "coffee"),
    ("v4-363", "query", "上月各区域的客单价",
     dsl("avg_order_value", dims=["region"], time=L1), "boss", "main", None),
    ("v4-364", "query", "上月各区域的客单价",
     dsl("avg_order_value", dims=["region"], time=L1), "boss", "main", "coffee"),
    ("v4-365", "query", "近6个月各城市的销量",
     dsl("item_quantity", dims=["city"], time=L6), "boss", "main", None),
    ("v4-366", "query", "近6个月各城市的销量",
     dsl("item_quantity", dims=["city"], time=L6), "boss", "main", "coffee"),
    ("v4-367", "query", "上月销售额最高的城市",
     dsl("revenue", dims=["city"], time=L1, direction="desc"), "boss", "main", None),
    ("v4-368", "query", "上月销售额最高的城市",
     dsl("revenue", dims=["city"], time=L1, direction="desc"), "boss", "main", "coffee"),
    ("v4-369", "query", "近12个月各区域的订单量",
     dsl("order_count", dims=["region"], time=L12), "boss", "main", None),
    ("v4-370", "query", "近12个月各区域的订单量",
     dsl("order_count", dims=["region"], time=L12), "boss", "main", "coffee"),
    ("v4-371", "query", "2025年3月至2025年8月各品类的毛利额",
     dsl("gross_margin", dims=["category"], time=BT("2025-03", "2025-08")), "boss", "main", None),
    ("v4-372", "query", "2025年3月至2025年8月各品类的毛利额",
     dsl("gross_margin", dims=["category"], time=BT("2025-03", "2025-08")), "boss", "main", "coffee"),
    ("v4-373", "query", "近6个月各渠道的毛利率",
     dsl("gross_margin_rate", dims=["channel"], time=L6), "boss", "main", None),
    ("v4-374", "query", "近6个月各渠道的毛利率",
     dsl("gross_margin_rate", dims=["channel"], time=L6), "boss", "main", "coffee"),
    # E. TopN 变体 ×15（limit 3/5/10 不同截断，含 top N 英文形态与 asc 截断）
    ("v4-375", "query", "上月净销售额前10的城市",
     dsl("net_revenue", dims=["city"], time=L1, limit=10), "boss", "main", None),
    ("v4-376", "query", "近3个月毛利额前3的省份",
     dsl("gross_margin", dims=["province"], time=L3, limit=3), "boss", "main", None),
    ("v4-377", "query", "上月订单量TOP5的城市",
     dsl("order_count", dims=["city"], time=L1, limit=5), "boss", "main", None),
    ("v4-378", "query", "近6个月销售额前10的细分品类",
     dsl("revenue", dims=["subcategory"], time=L6, limit=10), "boss", "main", None),
    ("v4-379", "query", "上月客单价前3的渠道",
     dsl("avg_order_value", dims=["channel"], time=L1, limit=3), "boss", "main", None),
    ("v4-380", "query", "近12个月销量前5的城市",
     dsl("item_quantity", dims=["city"], time=L12, limit=5), "boss", "main", None),
    ("v4-381", "query", "上月购买客户数前10的省份",
     dsl("customer_count", dims=["province"], time=L1, limit=10), "boss", "main", None),
    ("v4-382", "query", "近3个月订单量前3的大区",
     dsl("order_count", dims=["region"], time=L3, limit=3), "boss", "main", None),
    ("v4-383", "query", "上月毛利额前5的品类",
     dsl("gross_margin", dims=["category"], time=L1, limit=5), "boss", "main", None),
    ("v4-384", "query", "上月退货率最低的前3个渠道",
     dsl("refund_rate", dims=["channel"], time=L1, limit=3, direction="asc"), "boss", "main", None),
    ("v4-385", "query", "近3个月客单价最低的前5的城市",
     dsl("avg_order_value", dims=["city"], time=L3, limit=5, direction="asc"), "boss", "main", None),
    ("v4-386", "query", "上月销售额前5的城市",
     dsl("revenue", dims=["city"], time=L1, limit=5), "boss", "main", None),
    ("v4-387", "query", "近12个月购买客户数前10的城市",
     dsl("customer_count", dims=["city"], time=L12, limit=10), "boss", "main", None),
    ("v4-388", "query", "近6个月销量前5的省份",
     dsl("item_quantity", dims=["province"], time=L6, limit=5), "boss", "main", None),
    ("v4-389", "query", "上月毛利率前3的品类",
     dsl("gross_margin_rate", dims=["category"], time=L1, limit=3), "boss", "main", None),
    # F. 补充 v3 未覆盖组合 ×100
    # F1. 单指标×时间 ×20
    ("v4-390", "query", "近3个月净销售额是多少",
     dsl("net_revenue", time=L3), "boss", "main", None),
    ("v4-391", "query", "最近6个月毛利率是多少",
     dsl("gross_margin_rate", time=L6), "boss", "main", None),
    ("v4-392", "query", "近24个月订单量是多少",
     dsl("order_count", time=L24), "boss", "main", None),
    ("v4-393", "query", "最近24个月销量是多少",
     dsl("item_quantity", time=L24), "boss", "main", None),
    ("v4-394", "query", "近6个月购买客户数是多少",
     dsl("customer_count", time=L6), "boss", "main", None),
    ("v4-395", "query", "近3个月退货率是多少",
     dsl("refund_rate", time=L3, direction="asc"), "boss", "main", None),
    ("v4-396", "query", "最近12个月的销售额",
     dsl("revenue", time=L12), "boss", "main", None),
    ("v4-397", "query", "近6个月毛利额是多少",
     dsl("gross_margin", time=L6), "boss", "main", None),
    ("v4-398", "query", "近12个月净销售额是多少",
     dsl("net_revenue", time=L12), "boss", "main", None),
    ("v4-399", "query", "近24个月购买客户数",
     dsl("customer_count", time=L24), "boss", "main", None),
    ("v4-400", "query", "近3个月销量",
     dsl("item_quantity", time=L3), "boss", "main", None),
    ("v4-401", "query", "最近6个月的客单价",
     dsl("avg_order_value", time=L6), "boss", "main", None),
    ("v4-402", "query", "近12个月订单量",
     dsl("order_count", time=L12), "boss", "main", None),
    ("v4-403", "query", "近6个月退货率",
     dsl("refund_rate", time=L6, direction="asc"), "boss", "main", None),
    ("v4-404", "query", "近12个月毛利率",
     dsl("gross_margin_rate", time=L12), "boss", "main", None),
    ("v4-405", "query", "近24个月毛利额",
     dsl("gross_margin", time=L24), "boss", "main", None),
    ("v4-406", "query", "近3个月客单价",
     dsl("avg_order_value", time=L3), "boss", "main", None),
    ("v4-407", "query", "近12个月购买客户数",
     dsl("customer_count", time=L12), "boss", "main", None),
    ("v4-408", "query", "近24个月净销售额",
     dsl("net_revenue", time=L24), "boss", "main", None),
    ("v4-409", "query", "近24个月退货率",
     dsl("refund_rate", time=L24, direction="asc"), "boss", "main", None),
    # F2. 单维分组 ×25
    ("v4-410", "query", "各品类净销售额",
     dsl("net_revenue", dims=["category"]), "boss", "main", None),
    ("v4-411", "query", "各省份客单价",
     dsl("avg_order_value", dims=["province"]), "boss", "main", None),
    ("v4-412", "query", "各城市订单量",
     dsl("order_count", dims=["city"]), "boss", "main", None),
    ("v4-413", "query", "各会员等级净销售额",
     dsl("net_revenue", dims=["member_level"]), "boss", "main", None),
    ("v4-414", "query", "各细分品类订单量",
     dsl("order_count", dims=["subcategory"]), "boss", "main", None),
    ("v4-415", "query", "各渠道客单价",
     dsl("avg_order_value", dims=["channel"]), "boss", "main", None),
    ("v4-416", "query", "各大区净销售额",
     dsl("net_revenue", dims=["region"]), "boss", "main", None),
    ("v4-417", "query", "各区域购买客户数",
     dsl("customer_count", dims=["region"]), "boss", "main", None),
    ("v4-418", "query", "各省份毛利额",
     dsl("gross_margin", dims=["province"]), "boss", "main", None),
    ("v4-419", "query", "各城市净销售额",
     dsl("net_revenue", dims=["city"]), "boss", "main", None),
    ("v4-420", "query", "各品类订单量",
     dsl("order_count", dims=["category"]), "boss", "main", None),
    ("v4-421", "query", "各渠道净销售额",
     dsl("net_revenue", dims=["channel"]), "boss", "main", None),
    ("v4-422", "query", "各会员等级销量",
     dsl("item_quantity", dims=["member_level"]), "boss", "main", None),
    ("v4-423", "query", "各细分品类毛利额",
     dsl("gross_margin", dims=["subcategory"]), "boss", "main", None),
    ("v4-424", "query", "各省份销量",
     dsl("item_quantity", dims=["province"]), "boss", "main", None),
    ("v4-425", "query", "各城市购买客户数",
     dsl("customer_count", dims=["city"]), "boss", "main", None),
    ("v4-426", "query", "各会员等级订单量",
     dsl("order_count", dims=["member_level"]), "boss", "main", None),
    ("v4-427", "query", "上月各城市的订单量",
     dsl("order_count", dims=["city"], time=L1), "boss", "main", None),
    ("v4-428", "query", "近3个月各省份的净销售额",
     dsl("net_revenue", dims=["province"], time=L3), "boss", "main", None),
    ("v4-429", "query", "近6个月各渠道的订单量",
     dsl("order_count", dims=["channel"], time=L6), "boss", "main", None),
    ("v4-430", "query", "近12个月各品类的毛利额",
     dsl("gross_margin", dims=["category"], time=L12), "boss", "main", None),
    ("v4-431", "query", "近6个月各区域的客单价",
     dsl("avg_order_value", dims=["region"], time=L6), "boss", "main", None),
    ("v4-432", "query", "近3个月各会员等级的净销售额",
     dsl("net_revenue", dims=["member_level"], time=L3), "boss", "main", None),
    ("v4-433", "query", "上月各省份的销量",
     dsl("item_quantity", dims=["province"], time=L1), "boss", "main", None),
    ("v4-434", "query", "近12个月各城市的毛利率",
     dsl("gross_margin_rate", dims=["city"], time=L12), "boss", "main", None),
    # F3. 多维分组 ×10（dims 顺序对齐解析器 dim_keywords 遍历序；分组数 ≤100 防截断）
    ("v4-435", "query", "上月各区域各会员等级的净销售额",
     dsl("net_revenue", dims=["region", "member_level"], time=L1), "boss", "main", None),
    ("v4-436", "query", "近3个月各品类各会员等级的毛利额",
     dsl("gross_margin", dims=["category", "member_level"], time=L3), "boss", "main", None),
    ("v4-437", "query", "上月各渠道各会员等级的毛利率",
     dsl("gross_margin_rate", dims=["channel", "member_level"], time=L1), "boss", "main", None),
    ("v4-438", "query", "近6个月各区域各渠道的净销售额",
     dsl("net_revenue", dims=["region", "channel"], time=L6), "boss", "main", None),
    ("v4-439", "query", "近6个月各区域各品类的毛利率",
     dsl("gross_margin_rate", dims=["category", "region"], time=L6), "boss", "main", None),
    ("v4-440", "query", "近3个月各品类各渠道的订单量",
     dsl("order_count", dims=["category", "channel"], time=L3), "boss", "main", None),
    ("v4-441", "query", "近12个月各渠道各会员等级的销量",
     dsl("item_quantity", dims=["channel", "member_level"], time=L12), "boss", "main", None),
    ("v4-442", "query", "上月各品类各区域的购买客户数",
     dsl("customer_count", dims=["category", "region"], time=L1), "boss", "main", None),
    ("v4-443", "query", "近6个月各渠道各品类的毛利率",
     dsl("gross_margin_rate", dims=["category", "channel"], time=L6), "boss", "main", None),
    ("v4-444", "query", "上月各区域各渠道的毛利额",
     dsl("gross_margin", dims=["region", "channel"], time=L1), "boss", "main", None),
    # F4. 过滤×分组 ×15
    ("v4-445", "query", "上月食品品类各城市的销量",
     dsl("item_quantity", dims=["city"], filters=[("category", "食品")], time=L1), "boss", "main", None),
    ("v4-446", "query", "近3个月官网渠道各品类的毛利额",
     dsl("gross_margin", dims=["category"], filters=[("channel", "官网")], time=L3), "boss", "main", None),
    ("v4-447", "query", "上月华北地区各城市的订单量",
     dsl("order_count", dims=["city"], filters=[("region", "华北")], time=L1), "boss", "main", None),
    ("v4-448", "query", "近6个月黑卡会员各品类的净销售额",
     dsl("net_revenue", dims=["category"], filters=[("member_level", "黑卡")], time=L6), "boss", "main", None),
    ("v4-449", "query", "上月华东地区各细分品类的销售额",
     dsl("revenue", dims=["subcategory"], filters=[("region", "华东")], time=L1), "boss", "main", None),
    ("v4-450", "query", "近12个月家居品类各区域的销量",
     dsl("item_quantity", dims=["region"], filters=[("category", "家居")], time=L12), "boss", "main", None),
    ("v4-451", "query", "上月直播渠道各会员等级的客单价",
     dsl("avg_order_value", dims=["member_level"], filters=[("channel", "直播")], time=L1), "boss", "main", None),
    ("v4-452", "query", "近3个月银卡会员各区域的购买客户数",
     dsl("customer_count", dims=["region"], filters=[("member_level", "银卡")], time=L3), "boss", "main", None),
    ("v4-453", "query", "上月金卡会员各城市的毛利额",
     dsl("gross_margin", dims=["city"], filters=[("member_level", "金卡")], time=L1), "boss", "main", None),
    ("v4-454", "query", "近6个月西南地区各品类的净销售额",
     dsl("net_revenue", dims=["category"], filters=[("region", "西南")], time=L6), "boss", "main", None),
    ("v4-455", "query", "上月搜索渠道各品类的销量",
     dsl("item_quantity", dims=["category"], filters=[("channel", "搜索")], time=L1), "boss", "main", None),
    ("v4-456", "query", "近12个月美妆品类各城市的订单量",
     dsl("order_count", dims=["city"], filters=[("category", "美妆")], time=L12), "boss", "main", None),
    ("v4-457", "query", "上月西北地区各渠道的毛利额",
     dsl("gross_margin", dims=["channel"], filters=[("region", "西北")], time=L1), "boss", "main", None),
    ("v4-458", "query", "近3个月服饰品类各渠道的净销售额",
     dsl("net_revenue", dims=["channel"], filters=[("category", "服饰")], time=L3), "boss", "main", None),
    ("v4-459", "query", "上月运动品类各区域的毛利率",
     dsl("gross_margin_rate", dims=["region"], filters=[("category", "运动")], time=L1), "boss", "main", None),
    # F5. 趋势 ×20（month×维度 窗口分组数 ≤100 防截断边界）
    ("v4-460", "query", "近12个月各品类订单量趋势",
     dsl("order_count", dims=["month", "category"], time=L12), "boss", "main", None),
    ("v4-461", "query", "近6个月各渠道毛利额趋势",
     dsl("gross_margin", dims=["month", "channel"], time=L6), "boss", "main", None),
    ("v4-462", "query", "近12个月各区域销售额趋势",
     dsl("revenue", dims=["month", "region"], time=L12), "boss", "main", None),
    ("v4-463", "query", "近6个月各会员等级净销售额趋势",
     dsl("net_revenue", dims=["month", "member_level"], time=L6), "boss", "main", None),
    ("v4-464", "query", "近3个月各省份销量趋势",
     dsl("item_quantity", dims=["month", "province"], time=L3), "boss", "main", None),
    ("v4-465", "query", "近6个月各品类客单价趋势",
     dsl("avg_order_value", dims=["month", "category"], time=L6), "boss", "main", None),
    ("v4-466", "query", "近12个月各渠道订单量趋势",
     dsl("order_count", dims=["month", "channel"], time=L12), "boss", "main", None),
    ("v4-467", "query", "近3个月各细分品类销售额趋势",
     dsl("revenue", dims=["month", "subcategory"], time=L3), "boss", "main", None),
    ("v4-468", "query", "近6个月各区域毛利额趋势",
     dsl("gross_margin", dims=["month", "region"], time=L6), "boss", "main", None),
    ("v4-469", "query", "近12个月各会员等级销量趋势",
     dsl("item_quantity", dims=["month", "member_level"], time=L12), "boss", "main", None),
    ("v4-470", "query", "近3个月各城市购买客户数趋势",
     dsl("customer_count", dims=["month", "city"], time=L3), "boss", "main", None),
    ("v4-471", "query", "近12个月食品品类订单量趋势",
     dsl("order_count", dims=["month"], filters=[("category", "食品")], time=L12), "boss", "main", None),
    ("v4-472", "query", "近6个月家居品类毛利额趋势",
     dsl("gross_margin", dims=["month"], filters=[("category", "家居")], time=L6), "boss", "main", None),
    ("v4-473", "query", "近12个月华北地区销售额趋势",
     dsl("revenue", dims=["month"], filters=[("region", "华北")], time=L12), "boss", "main", None),
    ("v4-474", "query", "近6个月黑卡会员客单价趋势",
     dsl("avg_order_value", dims=["month"], filters=[("member_level", "黑卡")], time=L6), "boss", "main", None),
    ("v4-475", "query", "近12个月官网渠道毛利额趋势",
     dsl("gross_margin", dims=["month"], filters=[("channel", "官网")], time=L12), "boss", "main", None),
    ("v4-476", "query", "近6个月运动品类购买客户数趋势",
     dsl("customer_count", dims=["month"], filters=[("category", "运动")], time=L6), "boss", "main", None),
    ("v4-477", "query", "近3个月西南地区订单量趋势",
     dsl("order_count", dims=["month"], filters=[("region", "西南")], time=L3), "boss", "main", None),
    ("v4-478", "query", "近12个月服饰品类净销售额趋势",
     dsl("net_revenue", dims=["month"], filters=[("category", "服饰")], time=L12), "boss", "main", None),
    ("v4-479", "query", "近6个月银卡会员销量趋势",
     dsl("item_quantity", dims=["month"], filters=[("member_level", "银卡")], time=L6), "boss", "main", None),
    # F6. 极值/asc ×10
    ("v4-480", "query", "上月销售额最高的省份",
     dsl("revenue", dims=["province"], time=L1, direction="desc"), "boss", "main", None),
    ("v4-481", "query", "近3个月订单量最多的城市",
     dsl("order_count", dims=["city"], time=L3, direction="desc"), "boss", "main", None),
    ("v4-482", "query", "上月毛利率最高的大区",
     dsl("gross_margin_rate", dims=["region"], time=L1, direction="desc"), "boss", "main", None),
    ("v4-483", "query", "近6个月退货率最低的品类",
     dsl("refund_rate", dims=["category"], time=L6, direction="asc"), "boss", "main", None),
    ("v4-484", "query", "全部时间内销量最高的渠道",
     dsl("item_quantity", dims=["channel"], direction="desc"), "boss", "main", None),
    ("v4-485", "query", "全部时间内毛利额最高的品类",
     dsl("gross_margin", dims=["category"], direction="desc"), "boss", "main", None),
    ("v4-486", "query", "近12个月客单价最低的城市",
     dsl("avg_order_value", dims=["city"], time=L12, direction="asc"), "boss", "main", None),
    ("v4-487", "query", "上月净销售额最高的大区",
     dsl("net_revenue", dims=["region"], time=L1, direction="desc"), "boss", "main", None),
    ("v4-488", "query", "近3个月购买客户数最多的省份",
     dsl("customer_count", dims=["province"], time=L3, direction="desc"), "boss", "main", None),
    ("v4-489", "query", "上月销量最低的渠道",
     dsl("item_quantity", dims=["channel"], time=L1, direction="asc"), "boss", "main", None),
    # ---- drill ×37：归因/分析/对比 新维度组合（措辞必含 heavy 触发词）----
    ("v4-490", "drill", "为什么近6个月食品品类的销售额下滑了", None, "boss", "main", None),
    ("v4-491", "drill", "分析一下上月各区域的净销售额", None, "boss", "main", None),
    ("v4-492", "drill", "归因一下近12个月各渠道毛利额的变化", None, "boss", "main", None),
    ("v4-493", "drill", "对比上月各大区的客单价差异", None, "boss", "main", None),
    ("v4-494", "drill", "为什么上月银卡会员的客单价下滑了", None, "boss", "main", None),
    ("v4-495", "drill", "分析近3个月各城市的毛利率", None, "boss", "main", None),
    ("v4-496", "drill", "为什么近12个月官网渠道的订单量下降了", None, "boss", "main", None),
    ("v4-497", "drill", "对比近6个月各品类客单价的差异", None, "boss", "main", None),
    ("v4-498", "drill", "归因一下上月各会员等级销量的变化", None, "boss", "main", None),
    ("v4-499", "drill", "为什么上月运动品类的购买客户数下滑了", None, "boss", "main", None),
    ("v4-500", "drill", "分析一下近6个月各省份的净销售额", None, "boss", "main", None),
    ("v4-501", "drill", "对比上月各渠道退货率的差异", None, "boss", "main", None),
    ("v4-502", "drill", "为什么近3个月黑卡会员的销售额增长了", None, "boss", "main", None),
    ("v4-503", "drill", "归因一下近6个月各品类订单量的变化", None, "boss", "main", None),
    ("v4-504", "drill", "分析上月各渠道的净销售额结构", None, "boss", "main", None),
    ("v4-505", "drill", "为什么上月西南地区的净销售额下滑了", None, "boss", "main", None),
    ("v4-506", "drill", "对比近12个月各区域销量的差异", None, "boss", "main", None),
    ("v4-507", "drill", "为什么上月金卡会员的毛利额下滑了", None, "boss", "main", None),
    ("v4-508", "drill", "分析一下近3个月各渠道的销量结构", None, "boss", "main", None),
    ("v4-509", "drill", "归因一下上月食品品类客单价的变化", None, "boss", "main", None),
    ("v4-510", "drill", "为什么近6个月搜索渠道的销售额下滑了", None, "boss", "main", None),
    ("v4-511", "drill", "分析上月各城市的净销售额", None, "boss", "main", None),
    ("v4-512", "drill", "对比近3个月各会员等级毛利率的差异", None, "boss", "main", None),
    ("v4-513", "drill", "为什么上月家居品类的订单量增长了", None, "boss", "main", None),
    ("v4-514", "drill", "归因一下近12个月各品类毛利率的变化", None, "boss", "main", None),
    ("v4-515", "drill", "分析一下上月各省份的毛利额", None, "boss", "main", None),
    ("v4-516", "drill", "为什么近3个月社交渠道的客单价下滑了", None, "boss", "main", None),
    ("v4-517", "drill", "对比上月各细分品类销售额的差异", None, "boss", "main", None),
    ("v4-518", "drill", "为什么上月普通会员的订单量下滑了", None, "boss", "main", None),
    ("v4-519", "drill", "分析近6个月各城市的客单价", None, "boss", "main", None),
    ("v4-520", "drill", "归因一下近3个月各区域购买客户数的变化", None, "boss", "main", None),
    ("v4-521", "drill", "为什么上月服饰品类的毛利率下滑了", None, "boss", "main", None),
    ("v4-522", "drill", "对比近6个月各省份订单量的差异", None, "boss", "main", None),
    ("v4-523", "drill", "为什么上月官网渠道的销量增长了", None, "boss", "main", None),
    ("v4-524", "drill", "分析一下近12个月各渠道退货率的变化", None, "boss", "main", None),
    ("v4-525", "drill", "为什么上月西北地区的毛利额下滑了", None, "boss", "main", None),
    ("v4-526", "drill", "对比上月各城市购买客户数的差异", None, "boss", "main", None),
    # ---- clarify ×38：模糊表述新形态（不与冻结 38 条重复）----
    ("v4-527", "clarify", "生意好不好", None, "boss", "main", None),
    ("v4-528", "clarify", "整体生意表现好不好", None, "boss", "main", None),
    ("v4-529", "clarify", "上个月的整体情况", None, "boss", "main", None),
    ("v4-530", "clarify", "销售表现好不好", None, "boss", "main", None),
    ("v4-531", "clarify", "各品类的情况", None, "boss", "main", None),
    ("v4-532", "clarify", "哪些商品卖得好", None, "boss", "main", None),
    ("v4-533", "clarify", "帮我看看最近的经营数据", None, "boss", "main", None),
    ("v4-534", "clarify", "上个月的业绩表现", None, "boss", "main", None),
    ("v4-535", "clarify", "核心指标表现如何", None, "boss", "main", None),
    ("v4-536", "clarify", "最近的数据好不好", None, "boss", "main", None),
    ("v4-537", "clarify", "哪个大区表现比较突出", None, "boss", "main", None),
    ("v4-538", "clarify", "帮我总结一下现状", None, "boss", "main", None),
    ("v4-539", "clarify", "最近的销售业绩怎么样", None, "boss", "main", None),
    ("v4-540", "clarify", "上季度卖得如何", None, "boss", "main", None),
    ("v4-541", "clarify", "会员的表现怎么样", None, "boss", "main", None),
    ("v4-542", "clarify", "渠道表现好不好", None, "boss", "main", None),
    ("v4-543", "clarify", "哪个品类更受欢迎", None, "boss", "main", None),
    ("v4-544", "clarify", "大盘表现如何", None, "boss", "main", None),
    ("v4-545", "clarify", "最近的营收情况", None, "boss", "main", None),
    ("v4-546", "clarify", "上个月的经营状况", None, "boss", "main", None),
    ("v4-547", "clarify", "现在生意怎么样", None, "boss", "main", None),
    ("v4-548", "clarify", "销量表现怎么样", None, "boss", "main", None),
    ("v4-549", "clarify", "各渠道的销量怎么样", None, "boss", "main", None),
    ("v4-550", "clarify", "帮我讲讲最近的经营表现", None, "boss", "main", None),
    ("v4-551", "clarify", "看看这个月的情况", None, "boss", "main", None),
    ("v4-552", "clarify", "上个月的客户表现", None, "boss", "main", None),
    ("v4-553", "clarify", "品类表现如何", None, "boss", "main", None),
    ("v4-554", "clarify", "最近哪些产品值得关注", None, "boss", "main", None),
    ("v4-555", "clarify", "整体的销售数据好不好", None, "boss", "main", None),
    ("v4-556", "clarify", "最近的经营走势", None, "boss", "main", None),
    ("v4-557", "clarify", "上个月表现汇总", None, "boss", "main", None),
    ("v4-558", "clarify", "哪个城市的业绩更好", None, "boss", "main", None),
    ("v4-559", "clarify", "会员购买情况怎么样", None, "boss", "main", None),
    ("v4-560", "clarify", "最近的退货表现怎么样", None, "boss", "main", None),
    ("v4-561", "clarify", "上个月的盈利情况怎么样", None, "boss", "main", None),
    ("v4-562", "clarify", "帮我看看上个月的表现", None, "boss", "main", None),
    ("v4-563", "clarify", "各区域这个月表现怎么样", None, "boss", "main", None),
    ("v4-564", "clarify", "近期的成交情况", None, "boss", "main", None),
    # ---- refusal ×36：预测未来 12 / 未采集指标 12 / 非数据请求 12 ----
    ("v4-565", "refusal", "预测一下明年各品类的销售额", None, "boss", "main", None),
    ("v4-566", "refusal", "下个月各渠道的客单价会怎么走", None, "boss", "main", None),
    ("v4-567", "refusal", "预计下个季度的毛利额", None, "boss", "main", None),
    ("v4-568", "refusal", "未来三个月各城市的销量", None, "boss", "main", None),
    ("v4-569", "refusal", "明年的退货率会改善吗", None, "boss", "main", None),
    ("v4-570", "refusal", "帮我预测下个月的销售情况", None, "boss", "main", None),
    ("v4-571", "refusal", "下季度订单量预计有多少", None, "boss", "main", None),
    ("v4-572", "refusal", "未来半年购买客户数会增长吗", None, "boss", "main", None),
    ("v4-573", "refusal", "预计明年年初的销售额", None, "boss", "main", None),
    ("v4-574", "refusal", "未来各渠道毛利率会怎么变化", None, "boss", "main", None),
    ("v4-575", "refusal", "预估下个月的客单价", None, "boss", "main", None),
    ("v4-576", "refusal", "明年此时销量会达到多少", None, "boss", "main", None),
    ("v4-577", "refusal", "会员复购率的趋势", None, "boss", "main", None),
    ("v4-578", "refusal", "各城市的客户满意度对比", None, "boss", "main", None),
    ("v4-579", "refusal", "上月售后投诉率", None, "boss", "main", None),
    ("v4-580", "refusal", "新客留存表现如何", None, "boss", "main", None),
    ("v4-581", "refusal", "各渠道的好评率有多少", None, "boss", "main", None),
    ("v4-582", "refusal", "用户的NPS评分走势", None, "boss", "main", None),
    ("v4-583", "refusal", "老客复购率上月怎么样", None, "boss", "main", None),
    ("v4-584", "refusal", "物流满意度上月数据", None, "boss", "main", None),
    ("v4-585", "refusal", "投诉率最近三个月的变化", None, "boss", "main", None),
    ("v4-586", "refusal", "客户留存率是多少", None, "boss", "main", None),
    ("v4-587", "refusal", "商品评分最近怎么样", None, "boss", "main", None),
    ("v4-588", "refusal", "各会员等级的复购率对比", None, "boss", "main", None),
    ("v4-589", "refusal", "帮我写一封客户感谢信", None, "boss", "main", None),
    ("v4-590", "refusal", "生成一张上月数据的PPT", None, "boss", "main", None),
    ("v4-591", "refusal", "把这段分析翻译成英文", None, "boss", "main", None),
    ("v4-592", "refusal", "推荐几个运营优化的方向", None, "boss", "main", None),
    ("v4-593", "refusal", "评价一下本次大促的策划方案", None, "boss", "main", None),
    ("v4-594", "refusal", "帮我写份年终总结提纲", None, "boss", "main", None),
    ("v4-595", "refusal", "生成一段朋友圈营销文案", None, "boss", "main", None),
    ("v4-596", "refusal", "翻译这份英文对账单", None, "boss", "main", None),
    ("v4-597", "refusal", "帮我推荐一款报表工具", None, "boss", "main", None),
    ("v4-598", "refusal", "评价一下客服团队本季度的工作", None, "boss", "main", None),
    ("v4-599", "refusal", "写一首关于双十一的打油诗", None, "boss", "main", None),
    ("v4-600", "refusal", "生成下阶段的工作计划文档", None, "boss", "main", None),
]

BATCH_TAG = "1B6-AB-v4-expansion"

EXPECTED_NEW_MIX = {"query": 189, "drill": 37, "clarify": 38, "refusal": 36}
CROSS_SOURCE_PAIRS = 10  # 同题跨源对数（ecommerce + coffee 各一条）


def _spec_of(dataset: str | None):
    """条目数据源 → (解析用 spec, 编译/执行用库路径)。None = 默认电商源（行为不变）。"""
    if dataset is None:
        from app.data.registry import DEFAULT_SPEC

        return DEFAULT_SPEC, DEFAULT_SPEC.db_abs_path
    return COFFEE_SPEC, COFFEE_SPEC.db_abs_path


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def self_check_new_queries() -> list[str]:
    """构建期自检①：mock 解析器重答每道新 query 题，与金标同库执行比对（LIMIT 剥离口径）。

    跨源（coffee）条目：解析/编译/执行全部随 COFFEE_SPEC 与 B 库走。
    """
    problems: list[str] = []
    for sid, kind, question, spec, role, _split, dataset in NEW_SPECS:
        if kind != "query":
            continue
        parse_spec, db = _spec_of(dataset)
        gold_sql, _ = compile_sql(build_dsl(spec, parse_spec), role=role, spec=parse_spec)
        try:
            parsed = build_dsl(_parse_question(question, parse_spec), parse_spec)
            got_sql, _ = compile_sql(parsed, role=role, spec=parse_spec)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} [{role}/{dataset or 'ecommerce'}] mock 解析/编译失败: {e}\n    Q: {question}")
            continue
        gold_body, gold_limit = strip_limit(gold_sql)
        got_body, got_limit = strip_limit(got_sql)
        if got_limit != gold_limit:
            problems.append(f"{sid} [{role}/{dataset or 'ecommerce'}] LIMIT 不一致: got {got_limit!r} vs gold {gold_limit!r}\n    Q: {question}")
            continue
        try:
            ok = _canon(execute_sync(got_body, db_path=db).rows) == _canon(execute_sync(gold_body, db_path=db).rows)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}")
            continue
        if not ok:
            problems.append(
                f"{sid} [{role}/{dataset or 'ecommerce'}] mock 结果与金标不一致\n    Q: {question}\n    gold: {gold_sql}\n    got : {got_sql}"
            )
    return problems


def limit_invariant_check() -> list[str]:
    """构建期自检④（AB-29）：全部 query 金标的截断边界 strict 断言（limit_invariant.py）。

    覆盖冻结 300 条中的 query 题（gold_sql 逐字取自冻结源文件，判分库=默认电商库）
    + 新增 189 条（compile_sql 生成，跨源条目按 B 库断言）。
    断言口径 = 边界 strict 即可入库：允许无界行数 > LIMIT，只要截断边界无并列；
    并列即拒绝该条目入集（fail-fast 报条目 id）。
    """
    problems: list[str] = []
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))["items"]
    n_checked = 0
    for it in frozen:
        if it["kind"] == "query" and it.get("gold_sql"):
            n_checked += 1
            ok, detail = limit_boundary_strict(it["gold_sql"])
            if not ok:
                problems.append(f"{it['id']} [冻结] {detail}")
    for sid, kind, question, spec, role, _split, dataset in NEW_SPECS:
        if kind != "query":
            continue
        n_checked += 1
        parse_spec, db = _spec_of(dataset)
        gold_sql, _ = compile_sql(build_dsl(spec, parse_spec), role=role, spec=parse_spec)
        ok, detail = limit_boundary_strict(gold_sql, db_path=db)
        if not ok:
            problems.append(f"{sid} [新增/{dataset or 'ecommerce'}] {detail}")
    print(f"LIMIT 边界不变量：{n_checked} 条 query 金标逐条断言")
    return problems


def self_check_new_kinds() -> list[str]:
    """构建期自检②③：clarify/refusal 用 mock clarifier 实判 verdict（payload 随条目数据源走）；
    drill 断言 mock 可解析出合法 DSL 且路由为 heavy（full 链路是 drill 判分前提）。"""
    problems: list[str] = []
    for sid, kind, question, _spec, _role, _split, dataset in NEW_SPECS:
        if kind in ("clarify", "refusal"):
            verdict = _clarifier({"question": question, "dataset": dataset or "ecommerce"})["verdict"]
            expect = "refuse" if kind == "refusal" else "clarify"
            if verdict != expect:
                problems.append(f"{sid} [{kind}] mock verdict={verdict} 期望 {expect}: {question}")
        elif kind == "drill":
            parse_spec, _db = _spec_of(dataset)
            try:
                build_dsl(_parse_question(question, parse_spec), parse_spec)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{sid} [drill] mock 解析产出非法 DSL: {e}\n    Q: {question}")
                continue
            if route_node({"question": question, "events": []})["depth"] != "full":
                problems.append(f"{sid} [drill] 路由为 light（drill 判分依赖 full 链路）: {question}")
    return problems


def integrity_check() -> list[str]:
    """完整性校验：总数/冻结逐字/配比/id 连续无冲突/问题串零重复（跨源同题对白名单）/对完整。"""
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))["items"]
    frozen_qs = {i["question"].strip() for i in frozen}
    frozen_ids = {i["id"] for i in frozen}
    problems: list[str] = []
    seen: dict[tuple[str, str], str] = {}  # (数据源, question) -> id；跨源同题（对）允许
    for sid, kind, question, _spec, _role, _split, dataset in NEW_SPECS:
        q = question.strip()
        if q in frozen_qs:
            problems.append(f"{sid} 问题与冻结集重复: {question}")
        key = (dataset or "ecommerce", q)
        if key in seen:
            problems.append(f"{sid} 问题与新集同数据源重复: {question}（与 {seen[key]}）")
        seen[key] = sid
        if sid in frozen_ids:
            problems.append(f"{sid} id 与冻结集冲突")
    expected_ids = [f"v4-{n}" for n in range(301, 601)]
    actual_ids = [s[0] for s in NEW_SPECS]
    if actual_ids != expected_ids:
        problems.append("新条目 id 应连续 v4-301..v4-600（实际不符）")
    from collections import Counter

    stats = Counter(k for _i, k, *_r in NEW_SPECS)
    if len(NEW_SPECS) != 300 or dict(stats) != EXPECTED_NEW_MIX:
        problems.append(f"新增配比不符预期 189/37/38/36: {dict(stats)}（总 {len(NEW_SPECS)}）")
    if len(frozen) != 300:
        problems.append(f"冻结源应为 300 条，实际 {len(frozen)}")
    # 同题跨源对：coffee 条目必须恰有同问题串的 ecommerce 孪生（恰好 10 对）
    ecom_qs = {q for (ds, q) in seen if ds == "ecommerce"}
    coffee_qs = {q for (ds, q) in seen if ds == "coffee"}
    n_pairs = len(coffee_qs & ecom_qs)
    if len(coffee_qs) != CROSS_SOURCE_PAIRS or n_pairs != CROSS_SOURCE_PAIRS:
        orphan = sorted(coffee_qs - ecom_qs)
        problems.append(
            f"同题跨源对应恰好 {CROSS_SOURCE_PAIRS} 对且 ecommerce 孪生齐备"
            f"（实际 coffee {len(coffee_qs)} 条，成对 {n_pairs} 对，缺孪生: {orphan}）"
        )
    return problems


def strip_limit(sql: str) -> tuple[str, str]:
    """剥掉编译器恒定的尾行 LIMIT——与 app/eval/limit_invariant.strip_limit 同口径。"""
    from app.eval.limit_invariant import strip_limit as _strip

    return _strip(sql)


def build(path: Path | None) -> str:
    frozen_payload = json.loads(FROZEN.read_text(encoding="utf-8"))
    frozen_items = frozen_payload["items"]

    items: list[dict] = [dict(i) for i in frozen_items]   # 逐字保留（dict 层复制，值零改动）
    for sid, kind, question, spec, role, split, dataset in NEW_SPECS:
        item: dict = {"id": sid, "kind": kind, "question": question, "role": role, "split": split}
        if spec is not None:
            parse_spec, _db = _spec_of(dataset)
            item["gold_sql"] = compile_sql(build_dsl(spec, parse_spec), role=role, spec=parse_spec)[0]
        if dataset is not None:
            item["dataset"] = dataset
        item["batch"] = BATCH_TAG
        items.append(item)

    # 冻结条目逐 dict 相等校验（防任何漂移）+ 总数断言
    for src, out in zip(frozen_items, items[: len(frozen_items)]):
        if src != out:
            raise SystemExit(f"冻结条目漂移: {src.get('id')}")
    if len(items) != 600:
        raise SystemExit(f"总数断言失败：期望 600，实际 {len(items)}")

    from collections import Counter

    stats = Counter((i["kind"], i.get("dataset", "ecommerce")) for i in items)
    note = (
        "v4 扩容评测集（[1B6] 任务，2026-11-20）：冻结 v3_300 300 条逐字保留 + 新增 300 条 "
        f"（batch={BATCH_TAG}，金标由 compile_sql 生成、跨源条目 spec=COFFEE_SPEC，"
        "scripts/build_dataset_v4_600.py 可重建/自检）。"
        "新条目与冻结集问题串零重复；新 query 题全部通过构建期 mock 重答自检，"
        "clarify/refusal 经 mock clarifier 实判、drill 经解析+heavy 路由断言，"
        "全部 query 金标过 LIMIT 截断边界 strict 断言（AB-29）。"
        "新维度：双过滤 AND 组合 / role 变体金标（south/north） / 显式年月区间×分组 / "
        "同题跨源变体（10 对 ecommerce×coffee，条目带 dataset 字段）/ TopN 变体（limit 3/5/10）。"
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
    problems += self_check_new_kinds()
    problems += limit_invariant_check()
    if problems:
        print(f"自检未通过 {len(problems)} 条：")
        for p in problems:
            print("  -", p)
        return 1
    print("自检通过：新 query 题 mock 重答与金标同库一致（含 coffee 条目按 B 库比对）；"
          "clarify/refusal verdict 实判一致；drill 解析合法且路由 heavy；"
          "与冻结集零重复、id 连续无冲突、配比 189/37/38/36、同题跨源 10 对齐备；"
          "全部 378 条 query 金标过 LIMIT 截断边界 strict 断言（AB-29）")

    note = build(None if args.check else TARGET)
    print(note)
    if not args.check:
        print(f"已写入 {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
