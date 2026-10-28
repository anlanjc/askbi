"""评测入口：python scripts/run_eval.py [--ablation none|sloppy|no_qa]"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval.runner import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
