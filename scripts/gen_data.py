"""生成/重建演示数据库。

用法：
    python scripts/gen_data.py               # 默认 24 个月（约 20 万行订单明细）
    python scripts/gen_data.py --months 12   # 更小的库（快速体验）
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.data.generator import generate

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    s = get_settings()
    months = args.months or s.generator_months
    seed = args.seed if args.seed is not None else s.generator_seed
    s.db_abs_path.parent.mkdir(parents=True, exist_ok=True)
    stats = generate(s.db_abs_path, seed=seed, months=months, end_month=s.generator_end_month)
    print("数据生成完成：", stats)
