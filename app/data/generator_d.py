"""酒店酒旅数据生成器（数据集 D）：固定种子、固定截止月，保证评测可复现。

注入的"业务故事"信号（归因分析的素材，与电商 A / 咖啡 B / SaaS C 同设计哲学）：
1. 度假型酒店预订占比逐月上升（强制改派份额 20% → 40% 线性抬升，实测月度占比
   ≈0.52 → 0.62，商务型被挤压）——drill_down 按店型分解时"度假"是稳定的正贡献者；
2. 旅行社渠道毛利率系统性低于直销（渠道价 0.88 折，清洁/服务成本不随渠道变化）
   ——各渠道毛利率对比可复算、可下钻；
3. 暑期旺季（7-8 月 +25%）、国庆月（10 月 +15%）、淡季（12-1-2 月 -15%）。

物理模型（第四套，与订单/小票/账单都不同——按"间夜"计量的住宿业形态）：
- bookings 预订单（booking_id / hotel_id / channel / checkin_date / guest_id 可空）
- stay_lines 住宿行（room_type_id / rooms 房间数 / nights 晚数 / unit_price 每晚房价）
- hotels 酒店（hotel_name / city / region / hotel_type 商务·度假·精品）
- room_types 房型（type_name 大床·双床·套房·亲子 / base_price 每晚牌价 / room_cost 每晚服务成本）

诚实边界：D 数据集不采集取消/未入住（口径说明如实声明，预订单即成交），
每间可订房按入住晚数计价，无按人头收费。
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

DDL = """
CREATE OR REPLACE TABLE hotels(
  hotel_id INTEGER, hotel_name VARCHAR, city VARCHAR, region VARCHAR, hotel_type VARCHAR);
CREATE OR REPLACE TABLE room_types(
  room_type_id INTEGER, type_name VARCHAR, base_price DOUBLE, room_cost DOUBLE);
CREATE OR REPLACE TABLE bookings(
  booking_id INTEGER, hotel_id INTEGER, channel VARCHAR, checkin_date DATE, guest_id INTEGER);
CREATE OR REPLACE TABLE stay_lines(
  line_id INTEGER, booking_id INTEGER, room_type_id INTEGER,
  rooms INTEGER, nights INTEGER, unit_price DOUBLE);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(checkin_date);
CREATE INDEX IF NOT EXISTS idx_lines_booking ON stay_lines(booking_id);
"""

# 酒店分布：七大区（与全局行级权限 ROLE_SCOPES 同一划分）
REGION_CITIES = {
    "华南": ["深圳", "广州", "三亚"],
    "华东": ["上海", "杭州", "南京"],
    "华北": ["北京", "天津"],
    "华中": ["武汉", "长沙"],
    "西南": ["成都", "重庆"],
    "西北": ["西安"],
    "东北": ["沈阳", "大连"],
}
REGION_W = np.array([0.20, 0.24, 0.20, 0.12, 0.12, 0.06, 0.06])
N_HOTELS = 60
HOTEL_TYPES = ["商务", "度假", "精品"]

# (房型, 每晚牌价, 每晚服务成本)——成本率手工钉死（直销口径毛利率 ~60-66%）
ROOM_TYPES = [
    ("大床", 380.0, 152.0),
    ("双床", 420.0, 168.0),
    ("套房", 1280.0, 512.0),
    ("亲子", 680.0, 272.0),
]
CHANNELS = ["直销", "旅行社", "协议", "门店"]
CHANNEL_W = np.array([0.35, 0.30, 0.20, 0.15])
TRAVEL_AGENT_DISCOUNT = 0.88   # 故事信号②：旅行社渠道每晚房价 = 牌价 × 0.88，成本不变
ROOMS_W = np.array([0.60, 0.30, 0.10])       # 一单 1/2/3 间
NIGHTS_W = np.array([0.45, 0.30, 0.15, 0.10])  # 住 1-4 晚
GUEST_P = 0.55                # 有宾客登记（guest_id 非空）的预订单占比


def month_range(end_ym: str, months: int) -> list[str]:
    y, m = int(end_ym[:4]), int(end_ym[5:7])
    out = []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def generate_d(db_path: Path, seed: int = 42, months: int = 24, end_month: str = "2026-09") -> dict:
    rng = np.random.default_rng(seed)
    con = duckdb.connect(str(db_path))
    con.execute("BEGIN TRANSACTION")
    for stmt in DDL.strip().split(";\n"):
        if stmt.strip():
            con.execute(stmt)

    # --- 酒店 ---
    regions = list(REGION_CITIES)
    hotel_rows = []
    for hid in range(1, N_HOTELS + 1):
        region = str(regions[int(rng.choice(len(regions), p=REGION_W))])
        city = str(REGION_CITIES[region][int(rng.integers(0, len(REGION_CITIES[region])))])
        hotel_type = str(HOTEL_TYPES[int(rng.integers(0, len(HOTEL_TYPES)))])
        hotel_rows.append((hid, f"{city}{hotel_type}酒店{hid:03d}", city, region, hotel_type))
    con.executemany("INSERT INTO hotels VALUES (?, ?, ?, ?, ?)", hotel_rows)
    hotel_ids = np.array([r[0] for r in hotel_rows])
    # 度假酒店集中在华南/华东（海滨/古镇），商务型全国分布——drill 分解有真实结构
    resort_ids = np.array([r[0] for r in hotel_rows if r[4] == "度假"])

    # --- 房型 ---
    rt_rows = [(i + 1, name, price, cost) for i, (name, price, cost) in enumerate(ROOM_TYPES)]
    rt_ids = np.array([r[0] for r in rt_rows])
    price_of = {r[0]: r[2] for r in rt_rows}
    con.executemany("INSERT INTO room_types VALUES (?, ?, ?, ?)", rt_rows)

    # --- 预订单/住宿行 ---
    yms = month_range(end_month, months)
    bid = 0
    lid = 0
    for mi, ym in enumerate(yms):
        y, m = int(ym[:4]), int(ym[5:7])
        growth = 1.0 + 0.008 * mi
        season = 1.25 if m in (7, 8) else (1.15 if m == 10 else (0.85 if m in (12, 1, 2) else 1.0))
        n_bookings = int(420 * growth * season)
        days_in_month = calendar.monthrange(y, m)[1]
        day_idxs = rng.integers(0, days_in_month, n_bookings)
        hidx = rng.choice(len(hotel_ids), n_bookings)
        chidx = rng.choice(len(CHANNELS), n_bookings, p=CHANNEL_W)
        # 故事信号①：度假占比 20% → 40% 线性上升（把部分预订单强制改派到度假酒店）
        flag_r = 0.20 + 0.20 * mi / max(1, len(yms) - 1)
        n_resort = min(int(n_bookings * flag_r), n_bookings)
        if len(resort_ids) and n_resort:
            forced = rng.choice(n_bookings, size=n_resort, replace=False)
            hidx = hidx.copy()
            # hidx 存的是 hotel_ids 的位置下标，度假酒店 id → 位置须经 searchsorted 映射
            hidx[forced] = np.searchsorted(hotel_ids, rng.choice(resort_ids, size=n_resort))
        bookings, lines = [], []
        for k in range(n_bookings):
            bid += 1
            guest_id = int(rng.integers(10000, 99999)) if rng.random() < GUEST_P else None
            bookings.append((
                bid, int(hotel_ids[hidx[k]]), CHANNELS[chidx[k]],
                date(y, m, int(day_idxs[k]) + 1), guest_id,
            ))
            n_lines = 2 if rng.random() < 0.15 else 1
            line_rts = rng.choice(len(rt_ids), n_lines, replace=False)
            for ri in line_rts:
                rt_id = int(rt_ids[ri])
                rooms = int(rng.choice([1, 2, 3], p=ROOMS_W))
                nights = int(rng.choice([1, 2, 3, 4], p=NIGHTS_W))
                lid += 1
                unit_price = price_of[rt_id] * (1 + 0.1 * rng.random()) * (
                    TRAVEL_AGENT_DISCOUNT if CHANNELS[chidx[k]] == "旅行社" else 1.0
                )
                lines.append((lid, bid, rt_id, rooms, nights, round(unit_price, 2)))
        con.executemany("INSERT INTO bookings VALUES (?, ?, ?, ?, ?)", bookings)
        con.executemany("INSERT INTO stay_lines VALUES (?, ?, ?, ?, ?, ?)", lines)

    con.execute("COMMIT")
    stats = {
        "months": len(yms),
        "hotels": len(hotel_rows),
        "room_types": len(rt_rows),
        "bookings": bid,
        "stay_lines": lid,
        "db_path": str(db_path),
    }
    con.close()
    return stats


if __name__ == "__main__":
    from app.config import get_settings
    from app.data.registry import get_spec

    s = get_settings()
    p = get_spec("hotel").db_abs_path
    p.parent.mkdir(parents=True, exist_ok=True)
    print(generate_d(p, s.generator_seed, s.generator_months, s.generator_end_month))
