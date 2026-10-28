"""线下连锁咖啡数据生成器（数据集 B）：固定种子、固定截止月，保证评测可复现。

注入的"业务故事"信号（归因分析的素材，与电商 A 的 generator 同设计哲学）：
1. 外卖占比逐月上升（渠道结构迁移：15% → 45%，堂食被挤压）——drill_down 按渠道
   分解时"外卖"是稳定的正贡献者；
2. 烘焙品类毛利率系统性高于饮品（成本结构差异：饮品成本率 ~50%，烘焙 ~37%）
   ——各品类毛利率对比可复算、可下钻。

诚实边界：B 数据集不采集退款与折扣（口径说明已如实声明），小票均为有效成交。
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

DDL = """
CREATE OR REPLACE TABLE stores(
  store_id INTEGER, store_name VARCHAR, city VARCHAR,
  region VARCHAR, store_type VARCHAR);
CREATE OR REPLACE TABLE skus(
  sku_id INTEGER, sku_name VARCHAR, category VARCHAR,
  subcategory VARCHAR, unit_price DOUBLE, unit_cost DOUBLE);
CREATE OR REPLACE TABLE tickets(
  ticket_id INTEGER, store_id INTEGER, channel VARCHAR,
  sale_date DATE, member_id INTEGER);
CREATE OR REPLACE TABLE ticket_lines(
  line_id INTEGER, ticket_id INTEGER, sku_id INTEGER,
  quantity INTEGER, unit_price DOUBLE);
CREATE INDEX IF NOT EXISTS idx_tickets_date ON tickets(sale_date);
CREATE INDEX IF NOT EXISTS idx_lines_ticket ON ticket_lines(ticket_id);
"""

# 门店分布：七大区（与全局行级权限 ROLE_SCOPES 同一划分），城市为华东等多城
STORE_PLAN = {
    "华南": (["深圳", "广州"], 6),
    "华东": (["上海", "杭州", "南京"], 6),
    "华北": (["北京", "天津"], 4),
    "华中": (["武汉", "长沙"], 2),
    "西南": (["成都", "重庆"], 3),
    "西北": (["西安"], 1),
    "东北": (["沈阳", "大连"], 2),
}
STORE_TYPES = np.array(["旗舰店", "标准店", "店中店"])
STORE_TYPE_W = np.array([0.15, 0.60, 0.25])

# (品名, 品类, 细分, 单价, 单件原料成本)——成本率手工钉死，保证烘焙毛利率 > 饮品
SKUS = [
    ("经典拿铁", "饮品", "咖啡", 22.0, 10.8),
    ("生椰拿铁", "饮品", "咖啡", 24.0, 11.5),
    ("美式咖啡", "饮品", "咖啡", 16.0, 7.2),
    ("燕麦拿铁", "饮品", "咖啡", 26.0, 12.4),
    ("冷萃咖啡", "饮品", "咖啡", 23.0, 11.0),
    ("焦糖玛奇朵", "饮品", "咖啡", 27.0, 12.9),
    ("茉莉花茶", "饮品", "茶饮", 13.0, 5.7),
    ("杨枝甘露", "饮品", "茶饮", 21.0, 9.4),
    ("柠檬气泡茶", "饮品", "茶饮", 17.0, 7.5),
    ("黄油可颂", "烘焙", "面包", 15.0, 5.4),
    ("全麦贝果", "烘焙", "面包", 14.0, 5.0),
    ("蒜香法棍", "烘焙", "面包", 16.0, 5.9),
    ("提拉米苏", "烘焙", "蛋糕", 25.0, 9.0),
    ("芝士蛋糕", "烘焙", "蛋糕", 26.0, 9.4),
    ("葡式蛋挞", "烘焙", "酥点", 12.0, 4.1),
    ("榴莲酥", "烘焙", "酥点", 18.0, 6.2),
]
SKU_CAT_W = np.array([1 if s[1] == "饮品" else 0 for s in SKUS], dtype=float)
SKU_CAT_W = SKU_CAT_W / SKU_CAT_W.sum()          # 饮品优先被抽为第一件
CHANNELS = ["堂食", "外带", "外卖"]
N_MEMBERS = 4000


def month_range(end_ym: str, months: int) -> list[str]:
    y, m = int(end_ym[:4]), int(end_ym[5:7])
    out = []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def generate_b(db_path: Path, seed: int = 42, months: int = 24, end_month: str = "2026-09") -> dict:
    rng = np.random.default_rng(seed)
    con = duckdb.connect(str(db_path))
    con.execute("BEGIN TRANSACTION")
    for stmt in DDL.strip().split(";\n"):
        if stmt.strip():
            con.execute(stmt)

    # --- 门店 ---
    stores = []
    sid = 0
    for region, (cities, n) in STORE_PLAN.items():
        for k in range(n):
            sid += 1
            city = str(cities[int(rng.integers(0, len(cities)))])
            stores.append((
                sid, f"{city}{region}{'壹' if k == 0 else '贰' if k == 1 else '叁' if k == 2 else '肆'}号店",
                city, region, str(STORE_TYPES[rng.choice(3, p=STORE_TYPE_W)]),
            ))
    store_ids = np.array([s[0] for s in stores])
    store_w = np.full(len(stores), 1.0 / len(stores))
    con.executemany("INSERT INTO stores VALUES (?, ?, ?, ?, ?)", stores)

    # --- 商品 ---
    skus = [
        (i + 1, name, cat, sub, price,
         round(price * float(rng.uniform(cost / price - 0.01, cost / price + 0.01)), 2))
        for i, (name, cat, sub, price, cost) in enumerate(SKUS)
    ]
    drink_ids = np.array([s[0] for s in skus if s[2] == "饮品"])
    bake_ids = np.array([s[0] for s in skus if s[2] == "烘焙"])
    price_of = {s[0]: s[4] for s in skus}
    con.executemany("INSERT INTO skus VALUES (?, ?, ?, ?, ?, ?)", skus)

    # --- 小票/小票行 ---
    yms = month_range(end_month, months)
    tid = 0
    lid = 0
    total_lines = 0
    for mi, ym in enumerate(yms):
        y, m = int(ym[:4]), int(ym[5:7])
        growth = 1.0 + 0.010 * mi
        season = 1.12 if m in (7, 8) else (0.92 if m in (1, 2) else 1.0)  # 夏季冷饮旺季
        n_tickets = int(1150 * growth * season)
        days_in_month = calendar.monthrange(y, m)[1]
        day_idxs = rng.integers(0, days_in_month, n_tickets)
        sidx = rng.choice(len(store_ids), n_tickets, p=store_w)
        # 故事信号①：外卖占比 15% → 45% 线性上升，外带平稳（28%），堂食被挤压
        takeout_p = 0.15 + 0.30 * mi / max(1, len(yms) - 1)
        ch_p = np.array([0.72 - takeout_p, 0.28, takeout_p])  # 堂食/外带/外卖，恒和为 1
        chidx = rng.choice(3, n_tickets, p=ch_p / ch_p.sum())
        has_member = rng.random(n_tickets) < 0.48
        member_ids = rng.integers(1, N_MEMBERS + 1, n_tickets)
        tickets, lines = [], []
        for k in range(n_tickets):
            tid += 1
            tickets.append((
                tid, int(store_ids[sidx[k]]), CHANNELS[chidx[k]],
                date(y, m, int(day_idxs[k]) + 1),
                int(member_ids[k]) if has_member[k] else None,
            ))
            n_lines = int(rng.choice([1, 2, 3], p=[0.55, 0.30, 0.15]))
            for _ in range(n_lines):
                lid += 1
                # 首件倾向饮品（咖啡店主力），搭单在全部 SKU 上均匀抽
                if rng.random() < 0.72:
                    sku = int(drink_ids[rng.integers(0, len(drink_ids))])
                else:
                    sku = int(bake_ids[rng.integers(0, len(bake_ids))])
                qty = 2 if (rng.random() < 0.30 and sku <= len(drink_ids)) else 1
                lines.append((lid, tid, sku, qty, price_of[sku]))
        total_lines += len(lines)
        con.executemany("INSERT INTO tickets VALUES (?, ?, ?, ?, ?)", tickets)
        con.executemany("INSERT INTO ticket_lines VALUES (?, ?, ?, ?, ?)", lines)

    con.execute("COMMIT")
    stats = {
        "months": len(yms),
        "stores": len(stores),
        "skus": len(skus),
        "members_pool": N_MEMBERS,
        "tickets": tid,
        "ticket_lines": total_lines,
        "db_path": str(db_path),
    }
    con.close()
    return stats


if __name__ == "__main__":
    from app.config import get_settings
    from app.data.registry import get_spec

    s = get_settings()
    p = get_spec("coffee").db_abs_path
    p.parent.mkdir(parents=True, exist_ok=True)
    print(generate_b(p, s.generator_seed, s.generator_months, s.generator_end_month))
