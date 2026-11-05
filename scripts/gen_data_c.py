"""生成/重建数据集 C（B2B SaaS 订阅）数据库。

用法：
    python scripts/gen_data_c.py               # 24 个月，~1.5 万账单/明细
    python scripts/gen_data_c.py --months 12   # 更小的库（快速体验/测试）
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.data.generator_c import generate_c
from app.data.registry import get_spec

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--db", type=str, default=None, help="覆盖输出路径（默认 saas spec 的库）")
    args = ap.parse_args()

    s = get_settings()
    months = args.months or s.generator_months
    seed = args.seed if args.seed is not None else s.generator_seed
    target = Path(args.db) if args.db else get_spec("saas").db_abs_path
    target.parent.mkdir(parents=True, exist_ok=True)
    stats = generate_c(target, seed=seed, months=months, end_month=s.generator_end_month)
    print("数据集 C 生成完成：", stats)
