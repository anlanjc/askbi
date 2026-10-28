"""答案导出 CSV：标准库 csv 模块实现，零第三方依赖。

与 web/index.html 前端导出（answerToCsv）同构格式：
  元信息段（问题/角色/结论/SQL/口径/导出时间）→ 空行 → 数据表（columns + rows）

文本以 U+FEFF 开头（写入文件即 UTF-8 BOM），Excel 双击打开中文不乱码。
本模块刻意不 import app 内任何模块（纯函数，共享导入链零耦合），
API 层接线只需在拿到「最近一次答案」处调用 answer_to_csv。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

# Excel 兼容：BOM 置于文本首，编码为 utf-8 后即 EF BB BF
BOM = "\ufeff"

_META_FIELDS = ("问题", "角色", "结论", "SQL", "口径")


def csv_cell(v) -> str:
    """单格归一：None→空串、浮点 round(…, 2)（与前端 fmtNum 展示口径一致）。

    转义（逗号/引号/换行的引号包裹+内部双写）交给 csv.writer 的 QUOTE_MINIMAL，
    其产出与 web/index.html 前端 csvCell 的手工转义逐字节同构——本函数若再做一层
    包裹会与 writer 双重转义（引号变成字面量）。
    """
    if v is None:
        return ""
    if isinstance(v, float):
        v = round(v, 2)
    return str(v)


def answer_to_csv(
    answer: dict,
    question: str = "",
    role: str = "",
    generated_at: datetime | None = None,
) -> str:
    """把 /api/ask 的 final answer（dict）序列化为 CSV 文本（带 BOM 前缀）。

    answer 期望形态（与 API final 帧一致）：
      {summary, narrative?, sql?, caliber?, table?: {columns: [...], rows: [[...], ...]}}
    非查询类答案（clarify/refusal 无 table）导出元信息段即可，不报错。
    """
    ts = (generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    rows: list[list] = [
        ["问题", question],
        ["角色", role],
        ["结论", answer.get("summary", "")],
        ["SQL", answer.get("sql", "")],
        ["口径", answer.get("caliber", "")],
        ["导出时间", ts],
        [],
    ]
    table = answer.get("table") or {}
    columns = table.get("columns")
    if columns:
        rows.append(list(columns))
        rows.extend(list(r) for r in table.get("rows") or [])
    elif answer.get("message"):  # clarify / refusal：无表格，给一行说明兜底
        rows.append(["说明", answer["message"]])

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    # 逐格先过 csv_cell（浮点归一+转义），否则 csv.writer 会 str() 原始值绕过格式层
    writer.writerows([[csv_cell(v) for v in row] for row in rows])
    return BOM + buf.getvalue()


_FILENAME_SAFE = re.compile(r'[\\/:*?"<>|\s]+')


def export_filename(question: str = "", now: datetime | None = None) -> str:
    """导出文件名：askbi_<问题片段>_<YYYYMMDDHHMMSS>.csv；Windows 非法字符与空白折叠为 _。"""
    frag = _FILENAME_SAFE.sub("_", (question or "answer").strip())[:24].strip("_") or "answer"
    ts = (now or datetime.now()).strftime("%Y%m%d%H%M%S")
    return f"askbi_{frag}_{ts}.csv"
