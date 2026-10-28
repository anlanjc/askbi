"""电商数据生成器：固定种子、固定截止月，保证评测结果可复现。

注入的"业务故事"信号（归因分析的素材）：
1. 美妆品类自 2026-05 起折扣显著升高（促销拉扯）→ 毛利率下滑，全区域可下钻；
2. 直播渠道占比逐月上升（渠道结构迁移）；
3. 数码品类销售额稳定增长；服饰有明显季节性（11-12月、6月高峰）。
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

DDL = """
CREATE OR REPLACE TABLE products(
  product_id INTEGER, product_name VARCHAR, category VARCHAR,
  subcategory VARCHAR, supply_cost DOUBLE);
CREATE OR REPLACE TABLE customers(
  customer_id INTEGER, member_level VARCHAR, register_date DATE);
CREATE OR REPLACE TABLE orders(
  order_id INTEGER, order_date DATE, customer_id INTEGER,
  province VARCHAR, city VARCHAR, region VARCHAR, channel VARCHAR, status VARCHAR);
CREATE OR REPLACE TABLE order_items(
  order_id INTEGER, product_id INTEGER, quantity INTEGER,
  unit_price DOUBLE, discount DOUBLE);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
"""

CATALOG = {
    "数码": (["手机配件", "电脑外设", "智能穿戴", "影音设备"], 60.0, 1600.0, 0.28),
    "服饰": (["男装", "女装", "童装", "运动服饰"], 30.0, 320.0, 0.55),
    "美妆": (["护肤", "彩妆", "个护"], 25.0, 260.0, 0.62),
    "家居": (["收纳", "家纺", "厨具", "灯具"], 15.0, 420.0, 0.45),
    "食品": (["零食", "饮品", "粮油"], 8.0, 120.0, 0.38),
    "运动": (["健身器材", "户外装备", "球类"], 40.0, 680.0, 0.30),
}
REGIONS = {
    "华南": (["广东:广州", "广东:深圳", "福建:福州", "广西:南宁"], 0.30),
    "华东": (["上海:上海", "江苏:南京", "浙江:杭州", "安徽:合肥"], 0.26),
    "华北": (["北京:北京", "天津:天津", "河北:石家庄", "山西:太原"], 0.18),
    "华中": (["湖北:武汉", "湖南:长沙", "河南:郑州"], 0.10),
    "西南": (["四川:成都", "重庆:重庆", "云南:昆明"], 0.09),
    "西北": (["陕西:西安", "甘肃:兰州"], 0.04),
    "东北": (["辽宁:沈阳", "吉林:长春", "黑龙江:哈尔滨"], 0.03),
}
CHANNELS = ["直播", "搜索", "推荐", "社交", "官网"]
MEMBER_LEVELS = np.array(["普通", "银卡", "金卡", "黑卡"])
MEMBER_W = np.array([0.55, 0.25, 0.15, 0.05])


def month_range(end_ym: str, months: int) -> list[str]:
    y, m = int(end_ym[:4]), int(end_ym[5:7])
    out = []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def generate(db_path: Path, seed: int = 42, months: int = 24, end_month: str = "2026-09") -> dict:
    rng = np.random.default_rng(seed)
    con = duckdb.connect(str(db_path))
    con.execute("BEGIN TRANSACTION")
    for stmt in DDL.strip().split(";\n"):
        if stmt.strip():
            con.execute(stmt)

    # --- 商品 ---
    pid = 0
    products = []
    for cat, (subs, lo, hi, margin) in CATALOG.items():
        for sub in subs:
            for k in range(6):
                pid += 1
                cost = round(float(rng.uniform(lo, hi)), 2)
                # supply_cost 按品类毛利率基准反推
                supply = round(cost * (1 - margin * float(rng.uniform(0.85, 1.15))), 2)
                products.append((pid, f"{cat}-{sub}-{k + 1:02d}", cat, sub, supply))
    cat_of = {p[0]: p[2] for p in products}
    price_of = {p[0]: round(p[4] / max(1e-9, 1 - CATALOG[p[2]][3]), 2) for p in products}
    cat_weight = {c: CATALOG[c][3] for c in CATALOG}
    prod_ids = np.array([p[0] for p in products])
    prod_w = np.array([cat_weight[cat_of[i]] for i in prod_ids])
    prod_w = prod_w / prod_w.sum()
    con.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", products)

    # --- 客户 ---
    n_cust = 20000
    reg_dates = date(2023, 1, 1).toordinal() + rng.integers(0, 900, n_cust)
    customers = [
        (i + 1, MEMBER_LEVELS[rng.choice(4, p=MEMBER_W)], date.fromordinal(int(d)))
        for i, d in enumerate(reg_dates)
    ]
    con.executemany("INSERT INTO customers VALUES (?, ?, ?)", customers)

    # --- 订单/订单行 ---
    yms = month_range(end_month, months)
    region_names = list(REGIONS)
    region_p = np.array([REGIONS[r][1] for r in region_names])
    region_p = region_p / region_p.sum()
    oid = 0
    total_items = 0
    for mi, ym in enumerate(yms):
        y, m = int(ym[:4]), int(ym[5:7])
        growth = 1.0 + 0.012 * mi                       # 整体缓慢增长
        season = 1.35 if m in (11, 12) else (1.25 if m == 6 else (0.9 if m in (2, 3) else 1.0))
        n_orders = int(3600 * growth * season)
        days_in_month = calendar.monthrange(y, m)[1]
        day_idxs = rng.integers(0, days_in_month, n_orders)
        ridx = rng.choice(len(region_names), n_orders, p=region_p)
        # 渠道结构迁移：直播占比从 12% 升到 34%，官网被挤压但不低于 5%
        live_p = 0.12 + 0.22 * mi / max(1, len(yms) - 1)
        ch_p = np.array([live_p, 0.26, 0.24, 0.20, max(0.05, 0.70 - live_p)])
        ch_p = ch_p / ch_p.sum()
        chidx = rng.choice(len(CHANNELS), n_orders, p=ch_p)
        cust_idx = rng.integers(1, n_cust + 1, n_orders)
        r = rng.random(n_orders)
        status = np.where(r < 0.03, "已退货", np.where(r < 0.05, "已取消", "已完成"))
        orders, items = [], []
        for k in range(n_orders):
            oid += 1
            region = region_names[ridx[k]]
            province_city = REGIONS[region][0][rng.integers(0, len(REGIONS[region][0]))]
            province, city = province_city.split(":")
            orders.append((
                oid, date(y, m, int(day_idxs[k]) + 1), int(cust_idx[k]),
                province, city, region, CHANNELS[chidx[k]], str(status[k]),
            ))
            n_items = int(rng.choice([1, 2, 3, 4], p=[0.45, 0.3, 0.15, 0.1]))
            picks = rng.choice(prod_ids, n_items, p=prod_w)
            for pk in picks:
                cat = cat_of[int(pk)]
                # 故事信号：美妆自 2026-05 起折扣拉高（华南更早一个月），9 月促销进一步加码
                disc = rng.uniform(0.04, 0.12)
                if cat == "美妆" and ym >= ("2026-04" if region == "华南" else "2026-05"):
                    disc = rng.uniform(0.18, 0.30)
                if cat == "美妆" and ym >= "2026-09":
                    disc = rng.uniform(0.30, 0.35)
                if m in (6, 11, 12):
                    disc += rng.uniform(0.0, 0.06)  # 大促月整体折扣加深
                items.append((
                    oid, int(pk), int(rng.integers(1, 4)),
                    round(price_of[int(pk)] * float(rng.uniform(0.96, 1.05)), 2),
                    round(min(float(disc), 0.35), 3),
                ))
        total_items += len(items)
        con.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", orders)
        con.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", items)

    con.execute("COMMIT")
    stats = {
        "months": len(yms),
        "products": len(products),
        "customers": n_cust,
        "orders": oid,
        "order_items": total_items,
        "db_path": str(db_path),
    }
    con.close()
    return stats


if __name__ == "__main__":
    from app.config import get_settings

    s = get_settings()
    s.db_abs_path.parent.mkdir(parents=True, exist_ok=True)
    print(generate(s.db_abs_path, s.generator_seed, s.generator_months, s.generator_end_month))
