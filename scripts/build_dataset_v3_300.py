"""构建扩容评测集（eval/dataset_v3_300.json，[1B4-AB] 任务一）：冻结 v2_140 148 条逐字保留 + 新增 152 条。

纪律（对照 [1B3] 前批 scripts/build_dataset_v2_140.py 同款，任务卡 [1B4] W2 车道）：
- 冻结的 eval/dataset_v2_140.json 一个字节不改——本脚本只读它；构建产物内冻结条目
  与源文件**逐 dict 相等**（程序化校验，任何漂移立即报错），总数断言 300 条；
- 新条目金标一律 compile_sql 生成，禁止手写 SQL；
- 构建期自检（四重）：
  1. query：mock 解析器重答每道新 query 题，与金标同库执行比对（同 v2_140 口径）；
  2. clarify/refusal：mock clarifier 实判 verdict 与期望 kind 一致；
  3. drill：mock 解析可产出合法 DSL（build_dsl 不抛）且路由为 heavy（drill 判分
     依赖 full 链路的叙述+QA，light 链路必挂——措辞必须含重词）；
  4. query 金标 LIMIT 截断边界 strict 断言（AB-29，冻结+新增逐条 fail-fast）；
- 新题与冻结 148 题问题串零重复 + 新题集合内部零重复（程序化断言）；
- 新条目带 batch 字段标注批次来源，冻结条目原样（无该字段）。

新增 152 条分类配比（实读现有分布定）：v2_140 现状 query 93（62.8%）/ drill 18
（12.2%）/ clarify 19（12.8%）/ refusal 18（12.2%），按比例取整：
query 96 / drill 19 / clarify 19 / refusal 18 = 152。
措辞全部先经 mock 探针验证解析形态再定稿（探针-修正迭代记录见提交信息）。

用法（项目根，须用 askbi/.venv/Scripts/python.exe）：
    python scripts/build_dataset_v3_300.py            # 构建 + 自检，写入 eval/dataset_v3_300.json
    python scripts/build_dataset_v3_300.py --check    # 只自检 + 完整性校验，不写盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agents.nodes import route_node
from app.data.dsl import QueryDSL, build_dsl, compile_sql
from app.data.sandbox import execute_sync
from app.eval.limit_invariant import limit_boundary_strict
from app.llm.mock import _clarifier, _parse_question

FROZEN = ROOT / "eval" / "dataset_v2_140.json"
TARGET = ROOT / "eval" / "dataset_v3_300.json"


def dsl(
    metric: str,
    dims: list[str] | None = None,
    filters: list[tuple[str, object]] | None = None,
    time: dict | None = None,
    limit: int | None = None,
    direction: str | None = None,
) -> dict:
    """意图 DSL 简写构造器（与 build_dataset_v2_140.py 同形）。"""
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

# 新增条目：(id, kind, question, dsl | None, role, split)——措辞全部先经 mock 探针
# 验证解析形态再定稿（探针-修正记录见本批提交信息；构建期自检兜底任何回归）。
# 配比按 v2_140 现状分布取整：query 96 / drill 19 / clarify 19 / refusal 18。
NEW_SPECS: list[tuple[str, str, str, dict | None, str, str]] = [
    # ---- query ×96：冻结集未覆盖的 指标×维度×时间×过滤 组合 ----
    # A. 带过滤的单指标聚合 ×25
    ("v3-149", "query", "上月家居品类的毛利额",
     dsl("gross_margin", filters=[("category", "家居")], time=L1), "boss", "main"),
    ("v3-150", "query", "数码品类上月净销售额",
     dsl("net_revenue", filters=[("category", "数码")], time=L1), "boss", "main"),
    ("v3-151", "query", "食品品类最近12个月的销量",
     dsl("item_quantity", filters=[("category", "食品")], time=L12), "boss", "main"),
    ("v3-152", "query", "美妆品类最近12个月的订单量",
     dsl("order_count", filters=[("category", "美妆")], time=L12), "boss", "main"),
    ("v3-153", "query", "运动品类近12个月毛利率",
     dsl("gross_margin_rate", filters=[("category", "运动")], time=L12), "boss", "main"),
    ("v3-154", "query", "官网渠道最近12个月的销售额",
     dsl("revenue", filters=[("channel", "官网")], time=L12), "boss", "main"),
    ("v3-155", "query", "社交渠道上月毛利额",
     dsl("gross_margin", filters=[("channel", "社交")], time=L1), "boss", "main"),
    ("v3-156", "query", "推荐渠道近6个月客单价",
     dsl("avg_order_value", filters=[("channel", "推荐")], time=L6), "boss", "main"),
    ("v3-157", "query", "普通会员近6个月的购买客户数",
     dsl("customer_count", filters=[("member_level", "普通")], time=L6), "boss", "main"),
    ("v3-158", "query", "银卡会员上月订单量",
     dsl("order_count", filters=[("member_level", "银卡")], time=L1), "boss", "main"),
    ("v3-159", "query", "华北地区上月净销售额",
     dsl("net_revenue", filters=[("region", "华北")], time=L1), "boss", "main"),
    ("v3-160", "query", "西南地区上月客单价",
     dsl("avg_order_value", filters=[("region", "西南")], time=L1), "boss", "main"),
    ("v3-161", "query", "东北地区近3个月毛利额",
     dsl("gross_margin", filters=[("region", "东北")], time=L3), "boss", "main"),
    ("v3-162", "query", "华东地区近12个月净销售额",
     dsl("net_revenue", filters=[("region", "华东")], time=L12), "boss", "main"),
    ("v3-163", "query", "西北地区上月购买客户数",
     dsl("customer_count", filters=[("region", "西北")], time=L1), "boss", "main"),
    ("v3-164", "query", "华中地区近3个月退货率",
     dsl("refund_rate", filters=[("region", "华中")], time=L3), "boss", "main"),
    ("v3-165", "query", "上月直播渠道的毛利额",
     dsl("gross_margin", filters=[("channel", "直播")], time=L1), "boss", "main"),
    ("v3-166", "query", "美妆品类近3个月的净销售额",
     dsl("net_revenue", filters=[("category", "美妆")], time=L3), "boss", "main"),
    ("v3-167", "query", "金卡会员近12个月客单价",
     dsl("avg_order_value", filters=[("member_level", "金卡")], time=L12), "boss", "main"),
    ("v3-168", "query", "搜索渠道近12个月销量",
     dsl("item_quantity", filters=[("channel", "搜索")], time=L12), "boss", "main"),
    ("v3-169", "query", "家居品类近3个月订单量",
     dsl("order_count", filters=[("category", "家居")], time=L3), "boss", "main"),
    ("v3-170", "query", "食品品类上月购买客户数",
     dsl("customer_count", filters=[("category", "食品")], time=L1), "boss", "main"),
    ("v3-171", "query", "运动品类近6个月净销售额",
     dsl("net_revenue", filters=[("category", "运动")], time=L6), "boss", "main"),
    ("v3-172", "query", "直播渠道近6个月退货率",
     dsl("refund_rate", filters=[("channel", "直播")], time=L6), "boss", "main"),
    ("v3-173", "query", "华北地区上月购买客户数",
     dsl("customer_count", filters=[("region", "华北")], time=L1), "north", "main"),
    # B. 无过滤无维度的纯聚合 ×5
    ("v3-174", "query", "上月销量是多少",
     dsl("item_quantity", time=L1), "boss", "main"),
    ("v3-175", "query", "全部时间内净销售额",
     dsl("net_revenue"), "boss", "main"),
    ("v3-176", "query", "全部时间内毛利额",
     dsl("gross_margin"), "boss", "main"),
    ("v3-177", "query", "全部时间内购买客户数",
     dsl("customer_count"), "boss", "main"),
    ("v3-178", "query", "全部时间内客单价",
     dsl("avg_order_value"), "boss", "main"),
    # C. 单维分组 ×16
    ("v3-179", "query", "上月各品类毛利额",
     dsl("gross_margin", dims=["category"], time=L1), "boss", "main"),
    ("v3-180", "query", "上月各渠道销量",
     dsl("item_quantity", dims=["channel"], time=L1), "boss", "main"),
    ("v3-181", "query", "上月各省份毛利额",
     dsl("gross_margin", dims=["province"], time=L1), "boss", "main"),
    ("v3-182", "query", "上月各城市客单价",
     dsl("avg_order_value", dims=["city"], time=L1), "boss", "main"),
    ("v3-183", "query", "上月各细分品类净销售额",
     dsl("net_revenue", dims=["subcategory"], time=L1), "boss", "main"),
    ("v3-184", "query", "上月各会员等级毛利额",
     dsl("gross_margin", dims=["member_level"], time=L1), "boss", "main"),
    ("v3-185", "query", "上月各大区订单量",
     dsl("order_count", dims=["region"], time=L1), "boss", "main"),
    ("v3-186", "query", "最近3个月各城市净销售额",
     dsl("net_revenue", dims=["city"], time=L3), "boss", "main"),
    ("v3-187", "query", "最近3个月各省份购买客户数",
     dsl("customer_count", dims=["province"], time=L3), "boss", "main"),
    ("v3-188", "query", "最近6个月各会员等级销量",
     dsl("item_quantity", dims=["member_level"], time=L6), "boss", "main"),
    ("v3-189", "query", "最近3个月各细分品类客单价",
     dsl("avg_order_value", dims=["subcategory"], time=L3), "boss", "main"),
    ("v3-190", "query", "最近12个月各渠道毛利额",
     dsl("gross_margin", dims=["channel"], time=L12), "boss", "main"),
    ("v3-191", "query", "最近12个月各品类购买客户数",
     dsl("customer_count", dims=["category"], time=L12), "boss", "main"),
    ("v3-192", "query", "各渠道的退货率",
     dsl("refund_rate", dims=["channel"], direction="asc"), "boss", "main"),
    ("v3-193", "query", "各省份净销售额",
     dsl("net_revenue", dims=["province"]), "boss", "main"),
    ("v3-194", "query", "各城市毛利额",
     dsl("gross_margin", dims=["city"]), "boss", "main"),
    # D. 多维分组 ×5
    ("v3-195", "query", "上月各品类各渠道净销售额",
     dsl("net_revenue", dims=["category", "channel"], time=L1), "boss", "main"),
    ("v3-196", "query", "上月各渠道各会员等级销售额",
     dsl("revenue", dims=["channel", "member_level"], time=L1), "boss", "main"),
    ("v3-197", "query", "各渠道各品类销量",
     dsl("item_quantity", dims=["category", "channel"]), "boss", "main"),
    ("v3-198", "query", "上月各区域各品类订单量",
     dsl("order_count", dims=["category", "region"], time=L1), "boss", "main"),
    ("v3-199", "query", "最近6个月各品类各渠道毛利率",
     dsl("gross_margin_rate", dims=["category", "channel"], time=L6), "boss", "main"),
    # E. 过滤×分组组合 ×4
    ("v3-200", "query", "上月华南地区各品类销售额",
     dsl("revenue", dims=["category"], filters=[("region", "华南")], time=L1), "south", "main"),
    ("v3-201", "query", "上月美妆品类各渠道净销售额",
     dsl("net_revenue", dims=["channel"], filters=[("category", "美妆")], time=L1), "boss", "main"),
    ("v3-202", "query", "近3个月黑卡会员各渠道客单价",
     dsl("avg_order_value", dims=["channel"], filters=[("member_level", "黑卡")], time=L3), "boss", "main"),
    ("v3-203", "query", "近6个月家居品类各城市销量",
     dsl("item_quantity", dims=["city"], filters=[("category", "家居")], time=L6), "boss", "main"),
    # F. TopN/极值 ×14
    ("v3-204", "query", "净销售额最高的渠道",
     dsl("net_revenue", dims=["channel"], direction="desc"), "boss", "main"),
    ("v3-205", "query", "毛利额最高的细分品类",
     dsl("gross_margin", dims=["subcategory"], direction="desc"), "boss", "main"),
    ("v3-206", "query", "销量最高的会员等级",
     dsl("item_quantity", dims=["member_level"], direction="desc"), "boss", "main"),
    ("v3-207", "query", "客单价最高的城市",
     dsl("avg_order_value", dims=["city"], direction="desc"), "boss", "main"),
    ("v3-208", "query", "购买客户数最多的大区",
     dsl("customer_count", dims=["region"], direction="desc"), "boss", "main"),
    ("v3-209", "query", "订单量最多的省份",
     dsl("order_count", dims=["province"], direction="desc"), "boss", "main"),
    ("v3-210", "query", "退货率最高的渠道",
     dsl("refund_rate", dims=["channel"], direction="desc"), "boss", "main"),
    ("v3-211", "query", "上月退货率最低的渠道",
     dsl("refund_rate", dims=["channel"], time=L1, direction="asc"), "boss", "main"),
    ("v3-212", "query", "上月净销售额前5的城市",
     dsl("net_revenue", dims=["city"], time=L1, limit=5), "boss", "main"),
    ("v3-213", "query", "上月毛利额前5的会员等级",
     dsl("gross_margin", dims=["member_level"], time=L1, limit=5), "boss", "main"),
    ("v3-214", "query", "近3个月销量前5的品类",
     dsl("item_quantity", dims=["category"], time=L3, limit=5), "boss", "main"),
    ("v3-215", "query", "近3个月购买客户数排名前3的城市",
     dsl("customer_count", dims=["city"], time=L3, limit=3), "boss", "main"),
    ("v3-216", "query", "上月订单量最少的渠道",
     dsl("order_count", dims=["channel"], time=L1, direction="asc"), "boss", "main"),
    ("v3-217", "query", "上月销售额最低的省份",
     dsl("revenue", dims=["province"], time=L1, direction="asc"), "boss", "main"),
    # G. 趋势 ×17
    ("v3-218", "query", "近12个月毛利额趋势",
     dsl("gross_margin", dims=["month"], time=L12), "boss", "main"),
    ("v3-219", "query", "近12个月净销售额趋势",
     dsl("net_revenue", dims=["month"], time=L12), "boss", "main"),
    ("v3-220", "query", "近12个月退货率趋势",
     dsl("refund_rate", dims=["month"], time=L12, direction="asc"), "boss", "main"),
    ("v3-221", "query", "近12个月销量趋势",
     dsl("item_quantity", dims=["month"], time=L12), "boss", "main"),
    ("v3-222", "query", "近24个月销售额趋势",
     dsl("revenue", dims=["month"], time=L24), "boss", "main"),
    ("v3-223", "query", "近24个月客单价趋势",
     dsl("avg_order_value", dims=["month"], time=L24), "boss", "main"),
    ("v3-224", "query", "近6个月各品类销量趋势",
     dsl("item_quantity", dims=["month", "category"], time=L6), "boss", "main"),
    ("v3-225", "query", "近6个月各渠道客单价趋势",
     dsl("avg_order_value", dims=["month", "channel"], time=L6), "boss", "main"),
    ("v3-226", "query", "近6个月各会员等级毛利额趋势",
     dsl("gross_margin", dims=["month", "member_level"], time=L6), "boss", "main"),
    ("v3-227", "query", "近3个月各城市订单量趋势",
     dsl("order_count", dims=["month", "city"], time=L3), "boss", "main"),
    ("v3-228", "query", "近12个月华东地区销售额趋势",
     dsl("revenue", dims=["month"], filters=[("region", "华东")], time=L12), "boss", "main"),
    ("v3-229", "query", "近12个月社交渠道销量趋势",
     dsl("item_quantity", dims=["month"], filters=[("channel", "社交")], time=L12), "boss", "main"),
    ("v3-230", "query", "近12个月食品品类毛利率趋势",
     dsl("gross_margin_rate", dims=["month"], filters=[("category", "食品")], time=L12), "boss", "main"),
    ("v3-231", "query", "近3个月黑卡会员销量趋势",
     dsl("item_quantity", dims=["month"], filters=[("member_level", "黑卡")], time=L3), "boss", "main"),
    ("v3-232", "query", "近6个月家居品类净销售额趋势",
     dsl("net_revenue", dims=["month"], filters=[("category", "家居")], time=L6), "boss", "main"),
    ("v3-233", "query", "近6个月西南地区毛利额趋势",
     dsl("gross_margin", dims=["month"], filters=[("region", "西南")], time=L6), "boss", "main"),
    ("v3-234", "query", "近6个月华南地区订单量趋势",
     dsl("order_count", dims=["month"], filters=[("region", "华南")], time=L6), "boss", "main"),
    # H. 显式年月区间 ×10
    ("v3-235", "query", "2024年10月到2025年9月的销量",
     dsl("item_quantity", time=BT("2024-10", "2025-09")), "boss", "main"),
    ("v3-236", "query", "2025年1月到2025年12月的毛利额",
     dsl("gross_margin", time=BT("2025-01", "2025-12")), "boss", "main"),
    ("v3-237", "query", "2025年2月至2025年8月的客单价",
     dsl("avg_order_value", time=BT("2025-02", "2025-08")), "boss", "main"),
    ("v3-238", "query", "2025年3月到2025年5月各渠道销售额",
     dsl("revenue", dims=["channel"], time=BT("2025-03", "2025-05")), "boss", "main"),
    ("v3-239", "query", "2025年4月到2025年10月的净销售额",
     dsl("net_revenue", time=BT("2025-04", "2025-10")), "boss", "main"),
    ("v3-240", "query", "2024-10至2024-12的退货率",
     dsl("refund_rate", time=BT("2024-10", "2024-12"), direction="asc"), "boss", "main"),
    ("v3-241", "query", "2026年2月到2026年8月的购买客户数",
     dsl("customer_count", time=BT("2026-02", "2026-08")), "boss", "main"),
    ("v3-242", "query", "2025年6月到2026年5月各品类毛利额",
     dsl("gross_margin", dims=["category"], time=BT("2025-06", "2026-05")), "boss", "main"),
    ("v3-243", "query", "2025年9月至2026年2月各会员等级订单量",
     dsl("order_count", dims=["member_level"], time=BT("2025-09", "2026-02")), "boss", "main"),
    ("v3-244", "query", "2026年3月到2026年7月各渠道销量",
     dsl("item_quantity", dims=["channel"], time=BT("2026-03", "2026-07")), "boss", "main"),
    # ---- drill ×19：归因/分析/对比 新维度组合（措辞必含 heavy 触发词）----
    ("v3-245", "drill", "为什么上月家居品类的毛利额下滑了", None, "boss", "main"),
    ("v3-246", "drill", "分析一下近6个月各渠道的客单价", None, "boss", "main"),
    ("v3-247", "drill", "归因一下上月直播渠道的销量变化", None, "boss", "main"),
    ("v3-248", "drill", "对比上月各品类的毛利率差异", None, "boss", "main"),
    ("v3-249", "drill", "为什么近3个月社交渠道的订单量增长了", None, "boss", "main"),
    ("v3-250", "drill", "分析一下上月各会员等级的退货率结构", None, "boss", "main"),
    ("v3-251", "drill", "为什么上月官网渠道的净销售额下滑了", None, "boss", "main"),
    ("v3-252", "drill", "归因一下近6个月各城市的销量变化", None, "boss", "main"),
    ("v3-253", "drill", "对比近3个月各渠道的毛利额差异", None, "boss", "main"),
    ("v3-254", "drill", "为什么上月食品品类的客单价下滑了", None, "boss", "main"),
    ("v3-255", "drill", "分析一下最近6个月各细分品类的退货率", None, "boss", "main"),
    ("v3-256", "drill", "为什么上月华北地区的销售额下滑了", None, "boss", "main"),
    ("v3-257", "drill", "归因一下上月银卡会员的订单量变化", None, "boss", "main"),
    ("v3-258", "drill", "对比上月各会员等级的净销售额差异", None, "boss", "main"),
    ("v3-259", "drill", "为什么最近12个月数码品类的销量下滑了", None, "boss", "main"),
    ("v3-260", "drill", "分析一下上月各城市的购买客户数", None, "boss", "main"),
    ("v3-261", "drill", "为什么上月推荐渠道的毛利率下滑了", None, "boss", "main"),
    ("v3-262", "drill", "归因一下近3个月各品类客单价的变化", None, "boss", "main"),
    ("v3-263", "drill", "对比上月各省份的退货率差异", None, "boss", "main"),
    # ---- clarify ×19：模糊表述新形态（不与冻结 19 条重复）----
    ("v3-264", "clarify", "上周的经营情况怎么样", None, "boss", "main"),
    ("v3-265", "clarify", "这个月的整体表现怎么样", None, "boss", "main"),
    ("v3-266", "clarify", "哪个渠道比较给力", None, "boss", "main"),
    ("v3-267", "clarify", "会员情况怎么样", None, "boss", "main"),
    ("v3-268", "clarify", "帮我看看销售情况", None, "boss", "main"),
    ("v3-269", "clarify", "近期表现如何", None, "boss", "main"),
    ("v3-270", "clarify", "营业状况怎么样", None, "boss", "main"),
    ("v3-271", "clarify", "上个月哪些商品值得注意", None, "boss", "main"),
    ("v3-272", "clarify", "整体如何", None, "boss", "main"),
    ("v3-273", "clarify", "情况怎么样", None, "boss", "main"),
    ("v3-274", "clarify", "数据表现如何", None, "boss", "main"),
    ("v3-275", "clarify", "这个月生意怎么样", None, "boss", "main"),
    ("v3-276", "clarify", "各区域情况怎么样", None, "boss", "main"),
    ("v3-277", "clarify", "销量怎么样", None, "boss", "main"),
    ("v3-278", "clarify", "毛利率怎么样", None, "boss", "main"),
    ("v3-279", "clarify", "订单量表现怎么样", None, "boss", "main"),
    ("v3-280", "clarify", "客单价情况怎么样", None, "boss", "main"),
    ("v3-281", "clarify", "本月生意如何", None, "boss", "main"),
    ("v3-282", "clarify", "最近的数据怎么样", None, "boss", "main"),
    # ---- refusal ×18：预测未来 4 / 未采集指标 6 / 非数据请求 8----
    ("v3-283", "refusal", "预估下季度的销售额", None, "boss", "main"),
    ("v3-284", "refusal", "预测2027年的销量走势", None, "boss", "main"),
    ("v3-285", "refusal", "明年的订单量会怎么样", None, "boss", "main"),
    ("v3-286", "refusal", "未来12个月各品类毛利率趋势", None, "boss", "main"),
    ("v3-287", "refusal", "上个月的客户满意度是多少", None, "boss", "main"),
    ("v3-288", "refusal", "近6个月的复购率是多少", None, "boss", "main"),
    ("v3-289", "refusal", "上月用户留存情况", None, "boss", "main"),
    ("v3-290", "refusal", "各渠道的好评率对比", None, "boss", "main"),
    ("v3-291", "refusal", "客户投诉量上月有多少", None, "boss", "main"),
    ("v3-292", "refusal", "上个月的NPS评分是多少", None, "boss", "main"),
    ("v3-293", "refusal", "帮我写一段上月数据分析报告", None, "boss", "main"),
    ("v3-294", "refusal", "生成上月经营总结文档", None, "boss", "main"),
    ("v3-295", "refusal", "翻译一下这段SQL", None, "boss", "main"),
    ("v3-296", "refusal", "推荐几个值得投放的渠道", None, "boss", "main"),
    ("v3-297", "refusal", "评价一下上月各渠道的表现", None, "boss", "main"),
    ("v3-298", "refusal", "帮我预计下个月的毛利额", None, "boss", "main"),
    ("v3-299", "refusal", "帮我写封给老板的汇报邮件", None, "boss", "main"),
    ("v3-300", "refusal", "未来一年销量会增长吗", None, "boss", "main"),
]

BATCH_TAG = "1B4-AB-v3-expansion"


def _canon(rows: list[list]) -> list[tuple]:
    return sorted(
        tuple(round(v, 4) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in r)
        for r in rows
    )


def _strip_limit(sql: str) -> tuple[str, str]:
    """剥掉编译器恒定的尾行 LIMIT，返回 (主体 SQL, LIMIT 行)。

    分组行数 > LIMIT 时（如 省份×渠道=110 > 100），LIMIT 截断在并列值的边界上
    是**执行间非确定的**（DuckDB 并行聚合的并列组顺序漂移）——同一条 SQL 跑两次
    尾行都可能不同。自检改为：LIMIT 行逐字相等（parse 的 limit 语义仍被钉住）+
    无界全结果集比对（并列翻转不再造成假阳性）。分组行数与截断边界由构建期
    不变量 limit_boundary_strict 程序化保证（AB-29，见 limit_invariant_check）。
    """
    body, _, limit_line = sql.rpartition("\nLIMIT")
    return body, ("LIMIT" + limit_line)


def self_check_new_queries() -> list[str]:
    """构建期自检①：mock 解析器重答每道新 query 题，与金标同库执行比对（LIMIT 剥离口径）。"""
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
        gold_body, gold_limit = _strip_limit(gold_sql)
        got_body, got_limit = _strip_limit(got_sql)
        if got_limit != gold_limit:
            problems.append(f"{sid} [{role}] LIMIT 不一致: got {got_limit!r} vs gold {gold_limit!r}\n    Q: {question}")
            continue
        try:
            ok = _canon(execute_sync(got_body).rows) == _canon(execute_sync(gold_body).rows)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{sid} 执行比对失败: {e}\n    gold: {gold_sql}\n    got : {got_sql}")
            continue
        if not ok:
            problems.append(
                f"{sid} [{role}] mock 结果与金标不一致\n    Q: {question}\n    gold: {gold_sql}\n    got : {got_sql}"
            )
    return problems


def limit_invariant_check() -> list[str]:
    """构建期自检④（AB-29，评审 5.0）：全部 query 金标的截断边界 strict 断言。

    覆盖冻结 148 条中的 query 题（gold_sql 逐字取自冻结源文件，含已知 top-N 条目
    v2-033/036/039/124）+ 新增 96 条（compile_sql 生成，含 v3-212/214/215）。
    断言口径 = 边界 strict 即可入库：允许无界行数 > LIMIT，只要截断边界无并列
    （这正是 LIMIT 剥离口径的语义）；并列即拒绝该条目入集（fail-fast 报条目 id）。
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
    for sid, kind, question, spec, role, _split in NEW_SPECS:
        if kind != "query":
            continue
        n_checked += 1
        gold_sql, _ = compile_sql(QueryDSL(**spec), role=role)
        ok, detail = limit_boundary_strict(gold_sql)
        if not ok:
            problems.append(f"{sid} [新增] {detail}")
    print(f"LIMIT 边界不变量：{n_checked} 条 query 金标逐条断言")
    return problems


def self_check_new_kinds() -> list[str]:
    """构建期自检②③：clarify/refusal 用 mock clarifier 实判 verdict；
    drill 断言 mock 可解析出合法 DSL 且路由为 heavy（full 链路是 drill 判分前提）。"""
    problems: list[str] = []
    for sid, kind, question, _spec, _role, _split in NEW_SPECS:
        if kind in ("clarify", "refusal"):
            verdict = _clarifier({"question": question, "dataset": "ecommerce"})["verdict"]
            expect = "refuse" if kind == "refusal" else "clarify"
            if verdict != expect:
                problems.append(f"{sid} [{kind}] mock verdict={verdict} 期望 {expect}: {question}")
        elif kind == "drill":
            try:
                build_dsl(_parse_question(question))
            except Exception as e:  # noqa: BLE001
                problems.append(f"{sid} [drill] mock 解析产出非法 DSL: {e}\n    Q: {question}")
                continue
            if route_node({"question": question, "events": []})["depth"] != "full":
                problems.append(f"{sid} [drill] 路由为 light（drill 判分依赖 full 链路）: {question}")
    return problems


def integrity_check() -> list[str]:
    """完整性校验：与冻结 148 题问题串零重复；新题集合内部零重复；id 连续无冲突；配比正确。"""
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))["items"]
    frozen_qs = {i["question"].strip() for i in frozen}
    frozen_ids = {i["id"] for i in frozen}
    problems: list[str] = []
    seen_qs: set[str] = set()
    for sid, kind, question, _spec, _role, _split in NEW_SPECS:
        if question.strip() in frozen_qs:
            problems.append(f"{sid} 问题与冻结集重复: {question}")
        if question.strip() in seen_qs:
            problems.append(f"{sid} 问题与新集内部重复: {question}")
        seen_qs.add(question.strip())
        if sid in frozen_ids:
            problems.append(f"{sid} id 与冻结集冲突")
    from collections import Counter

    stats = Counter(k for _i, k, *_r in NEW_SPECS)
    if len(NEW_SPECS) != 152 or dict(stats) != {"query": 96, "drill": 19, "clarify": 19, "refusal": 18}:
        problems.append(f"新增配比不符预期 96/19/19/18: {dict(stats)}（总 {len(NEW_SPECS)}）")
    if len(frozen) != 148:
        problems.append(f"冻结源应为 148 条，实际 {len(frozen)}")
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

    # 冻结条目逐 dict 相等校验（防任何漂移）+ 总数断言
    for src, out in zip(frozen_items, items[: len(frozen_items)]):
        if src != out:
            raise SystemExit(f"冻结条目漂移: {src.get('id')}")
    if len(items) != 300:
        raise SystemExit(f"总数断言失败：期望 300，实际 {len(items)}")

    from collections import Counter

    stats = Counter((i["kind"], i["split"]) for i in items)
    note = (
        "v3 扩容评测集（[1B4-AB] 任务一，2026-11-17）：冻结 v2_140 148 条逐字保留 + 新增 152 条 "
        f"（batch={BATCH_TAG}，金标由 compile_sql 生成，scripts/build_dataset_v3_300.py 可重建/自检）。"
        "新条目与冻结集问题串零重复；新 query 题全部通过构建期 mock 重答自检，"
        "clarify/refusal 经 mock clarifier 实判、drill 经解析+heavy 路由断言。"
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
    print("自检通过：新 query 题 mock 重答与金标同库一致；clarify/refusal verdict 实判一致；"
          "drill 解析合法且路由 heavy；与冻结集零重复、id 无冲突、配比 96/19/19/18；"
          "全部 query 金标过 LIMIT 截断边界 strict 断言（AB-29）")

    note = build(None if args.check else TARGET)
    print(note)
    if not args.check:
        print(f"已写入 {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
