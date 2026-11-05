"""SaaS 订阅数据生成器（数据集 C）：固定种子、固定截止月，保证评测可复现。

注入的"业务故事"信号（归因分析的素材，与电商 A / 咖啡 B 同设计哲学）：
1. 旗舰版收入占比逐月上升（套餐档位结构迁移：20% → 40%，基础版被挤压）——
   drill_down 按档位分解时"旗舰版"是稳定的正贡献者；
2. 代理渠道毛利率系统性低于直销（渠道折扣：代理席位单价 = 价目表 × 0.85，
   成本不随渠道变化）——各渠道毛利率对比可复算、可下钻；
3. 年末预算季（11-12 月）新签账单放量（+15%）、春节月（1-2 月）回落（-10%）。

诚实边界：C 数据集不采集退款与按席位阶梯定价（口径说明已如实声明），
每张账单一行订阅明细，均为有效计费。
"""

from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

DDL = """
CREATE OR REPLACE TABLE companies(
  company_id INTEGER, company_name VARCHAR, industry VARCHAR, region VARCHAR);
CREATE OR REPLACE TABLE plans(
  plan_id INTEGER, plan_name VARCHAR, tier VARCHAR,
  seat_price DOUBLE, seat_cost DOUBLE);
CREATE OR REPLACE TABLE invoices(
  invoice_id INTEGER, company_id INTEGER, channel VARCHAR, invoice_date DATE);
CREATE OR REPLACE TABLE invoice_lines(
  line_id INTEGER, invoice_id INTEGER, plan_id INTEGER,
  seats INTEGER, unit_price DOUBLE);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_lines_invoice ON invoice_lines(invoice_id);
"""

# 客户分布：七大区（与全局行级权限 ROLE_SCOPES 同一划分）
REGION_CITIES = {
    "华南": ["深圳", "广州"],
    "华东": ["上海", "杭州", "南京"],
    "华北": ["北京", "天津"],
    "华中": ["武汉", "长沙"],
    "西南": ["成都", "重庆"],
    "西北": ["西安"],
    "东北": ["沈阳", "大连"],
}
REGION_W = np.array([0.20, 0.24, 0.20, 0.12, 0.12, 0.06, 0.06])
N_COMPANIES = 240
INDUSTRIES = np.array(["制造", "互联网", "零售", "金融", "教育"])
INDUSTRY_W = np.array([0.26, 0.30, 0.20, 0.14, 0.10])
NAME_SUFFIX = ["科技", "信息", "制造", "贸易", "服务"]

# (套餐名, 档位, 席位月价, 席位服务成本)——成本率手工钉死（直销口径毛利率 ~66-70%）
PLANS = [
    ("基础协作", "基础版", 39.0, 12.6),
    ("基础分析", "基础版", 49.0, 16.2),
    ("专业协作", "专业版", 99.0, 32.7),
    ("专业分析", "专业版", 119.0, 39.3),
    ("旗舰协作", "旗舰版", 199.0, 65.7),
    ("旗舰分析", "旗舰版", 259.0, 85.5),
]
CHANNELS = ["直销", "代理", "自助"]
CHANNEL_W = np.array([0.50, 0.30, 0.20])
AGENT_DISCOUNT = 0.85   # 故事信号②：代理渠道席位单价 = 价目表 × 0.85，成本不变
SEAT_RANGE = {"基础版": (20, 80), "专业版": (30, 120), "旗舰版": (50, 200)}


def month_range(end_ym: str, months: int) -> list[str]:
    y, m = int(end_ym[:4]), int(end_ym[5:7])
    out = []
    for _ in range(months):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def generate_c(db_path: Path, seed: int = 42, months: int = 24, end_month: str = "2026-09") -> dict:
    rng = np.random.default_rng(seed)
    con = duckdb.connect(str(db_path))
    con.execute("BEGIN TRANSACTION")
    for stmt in DDL.strip().split(";\n"):
        if stmt.strip():
            con.execute(stmt)

    # --- 客户公司 ---
    regions = list(REGION_CITIES)
    comp_rows = []
    for cid in range(1, N_COMPANIES + 1):
        region = str(regions[int(rng.choice(len(regions), p=REGION_W))])
        city = str(REGION_CITIES[region][int(rng.integers(0, len(REGION_CITIES[region])))])
        industry = str(INDUSTRIES[int(rng.choice(len(INDUSTRIES), p=INDUSTRY_W))])
        suffix = NAME_SUFFIX[cid % len(NAME_SUFFIX)]
        comp_rows.append((cid, f"{city}{suffix}{cid:03d}", industry, region))
    comp_ids = np.array([r[0] for r in comp_rows])
    con.executemany("INSERT INTO companies VALUES (?, ?, ?, ?)", comp_rows)

    # --- 套餐 ---
    plan_rows = [(i + 1, name, tier, price, cost) for i, (name, tier, price, cost) in enumerate(PLANS)]
    tier_plan_ids = {
        tier: np.array([r[0] for r in plan_rows if r[2] == tier]) for tier in SEAT_RANGE
    }
    price_of = {r[0]: r[3] for r in plan_rows}
    con.executemany("INSERT INTO plans VALUES (?, ?, ?, ?, ?)", plan_rows)

    # --- 账单/账单行 ---
    yms = month_range(end_month, months)
    iid = 0
    lid = 0
    for mi, ym in enumerate(yms):
        y, m = int(ym[:4]), int(ym[5:7])
        growth = 1.0 + 0.008 * mi
        season = 1.15 if m in (11, 12) else (0.90 if m in (1, 2) else 1.0)  # 故事信号③：预算季放量
        n_invoices = int(560 * growth * season)
        days_in_month = calendar.monthrange(y, m)[1]
        day_idxs = rng.integers(0, days_in_month, n_invoices)
        cidx = rng.choice(len(comp_ids), n_invoices)
        chidx = rng.choice(len(CHANNELS), n_invoices, p=CHANNEL_W)
        # 故事信号①：旗舰版占比 20% → 40% 线性上升，基础版 50% → 30% 被挤压
        flag_p = 0.20 + 0.20 * mi / max(1, len(yms) - 1)
        tier_p = np.array([0.50 - (flag_p - 0.20), 0.30, flag_p])  # 基础/专业/旗舰，恒和为 1
        tier_idx = rng.choice(3, n_invoices, p=tier_p / tier_p.sum())
        invoices, lines = [], []
        for k in range(n_invoices):
            iid += 1
            invoices.append((
                iid, int(comp_ids[cidx[k]]), CHANNELS[chidx[k]],
                date(y, m, int(day_idxs[k]) + 1),
            ))
            tier = ["基础版", "专业版", "旗舰版"][tier_idx[k]]
            plan_id = int(tier_plan_ids[tier][rng.integers(0, len(tier_plan_ids[tier]))])
            lo, hi = SEAT_RANGE[tier]
            seats = int(rng.integers(lo, hi + 1))
            lid += 1
            unit_price = price_of[plan_id] * (AGENT_DISCOUNT if CHANNELS[chidx[k]] == "代理" else 1.0)
            lines.append((lid, iid, plan_id, seats, unit_price))
        con.executemany("INSERT INTO invoices VALUES (?, ?, ?, ?)", invoices)
        con.executemany("INSERT INTO invoice_lines VALUES (?, ?, ?, ?, ?)", lines)

    con.execute("COMMIT")
    stats = {
        "months": len(yms),
        "companies": len(comp_rows),
        "plans": len(plan_rows),
        "invoices": iid,
        "invoice_lines": lid,
        "db_path": str(db_path),
    }
    con.close()
    return stats


if __name__ == "__main__":
    from app.config import get_settings
    from app.data.registry import get_spec

    s = get_settings()
    p = get_spec("saas").db_abs_path
    p.parent.mkdir(parents=True, exist_ok=True)
    print(generate_c(p, s.generator_seed, s.generator_months, s.generator_end_month))
