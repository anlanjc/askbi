"""导出 CSV 纯函数测试：转义/编码/同构/健壮性（零 app 内部依赖，AB-3 中间态安全）。"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from app.export import BOM, answer_to_csv, csv_cell, export_filename

ANSWER = {
    "summary": "上月美妆品类销售额 12.34 万元",
    "sql": "SELECT region, SUM(net_revenue) FROM sales GROUP BY region",
    "caliber": "净额口径；时间窗 2026-09-01 ~ 2026-09-30",
    "table": {
        "columns": ["区域", "净销售额", "毛利率"],
        "rows": [
            ["华南", 123456.789, 0.30000000000000004],
            ["华北", 98765.4, 0.45],
        ],
    },
}


def test_bom_prefix_and_meta_section():
    text = answer_to_csv(ANSWER, question="上月各区域销售额", role="boss",
                         generated_at=datetime(2026, 10, 29, 10, 0, 0))
    assert text.startswith(BOM)
    parsed = list(csv.reader(io.StringIO(text.lstrip(BOM), newline="")))
    # 解析式断言：值含逗号（如 SQL）时 csv 模块会加引号包裹，文本比对会误报
    assert parsed[0] == ["问题", "上月各区域销售额"]
    assert parsed[1] == ["角色", "boss"]
    assert parsed[2] == ["结论", "上月美妆品类销售额 12.34 万元"]
    assert parsed[3] == ["SQL", "SELECT region, SUM(net_revenue) FROM sales GROUP BY region"]
    assert parsed[4][0] == "口径"
    assert parsed[5] == ["导出时间", "2026-10-29 10:00:00"]
    assert parsed[6] == []                          # 元信息段与表格之间空行


def test_table_roundtrip_via_csv_reader():
    """csv.reader 读回解析：转义后逐格还原，行数 = 元信息 6 + 空行 1 + 表头 + 数据。"""
    text = answer_to_csv(ANSWER, question="q", role="boss",
                         generated_at=datetime(2026, 10, 29, 10, 0, 0))
    parsed = list(csv.reader(io.StringIO(text.lstrip(BOM), newline="")))
    assert parsed[5] == ["导出时间", "2026-10-29 10:00:00"]
    header, rows = parsed[7], parsed[8:]
    assert header == ["区域", "净销售额", "毛利率"]
    assert len(rows) == 2
    assert rows[0][0] == "华南"
    assert float(rows[0][1]) == 123456.79   # round(…, 2) 落到导出层
    assert float(rows[0][2]) == 0.3         # 浮点尾差被归一


def test_injection_cells_escaped_and_roundtrip():
    """逗号/引号/换行注入：加引号包裹+内部引号双写，读回还原原值。"""
    tricky = {
        "summary": "s",
        "table": {"columns": ["a", "b"],
                  "rows": [["x,y", 'he said "hi"'], ["line1\nline2", "normal"]]},
    }
    text = answer_to_csv(tricky, question="注入", role="boss")
    parsed = list(csv.reader(io.StringIO(text.lstrip(BOM), newline="")))
    body = parsed[8:]
    assert body[0] == ["x,y", 'he said "hi"']
    assert body[1] == ["line1\nline2", "normal"]


def test_line_terminator_crlf_and_frontend_parity():
    """行终止符 \r\n：与 web/index.html 前端 answerToCsv 的 join("\r\n") 同构。"""
    text = answer_to_csv(ANSWER, question="q", role="boss")
    body = text.lstrip(BOM)
    assert "\r\n" in body
    assert body.replace("\r\n", "").count("\n") == 0  # 没有裸 \n


def test_answer_without_table_does_not_crash():
    for kind_answer in ({"kind": "refusal", "message": "指标不存在"},
                        {"kind": "clarify", "message": "想看什么指标？", "options": ["销售额"]},
                        {}):
        text = answer_to_csv(kind_answer, question="q", role="boss")
        assert text.startswith(BOM)
        assert "\r\n" in text  # 元信息段仍在，导出可用


def test_export_filename_sanitized():
    ts = datetime(2026, 10, 29, 10, 15, 0)
    name = export_filename("上月 各区域/销售额:*?", now=ts)
    assert name == "askbi_上月_各区域_销售额_20261029101500.csv"
    assert not any(c in name for c in '\\/:*?"<>| ')
    assert export_filename("", now=ts) == "askbi_answer_20261029101500.csv"


def test_csv_cell_normalization_and_escape_parity():
    """归一在 csv_cell、转义在 csv.writer（QUOTE_MINIMAL）；组合产出与前端 csvCell 手工转义字节一致。"""
    assert csv_cell(None) == ""
    assert csv_cell(0.30000000000000004) == "0.3"
    assert csv_cell(123456.789) == "123456.79"
    assert csv_cell("plain") == "plain"
    assert csv_cell("a,b") == "a,b"  # 后端归一层不转义
    # 组合层与前端语义对齐：前端 csvCell('a,b') 产出 "a,b"（引号包裹），writer 同样产出
    text = answer_to_csv({"summary": "s", "table": {"columns": ["c"], "rows": [["a,b"], ['q"q'], ["n\nl"]]}},
                         question="q", role="boss")
    body = text.lstrip(BOM).split("\r\n")
    assert body[8] == '"a,b"'          # 前端：'"' + s.replace('"','""') + '"'
    assert body[9] == '"q""q"'
    assert body[10] == '"n\nl"'
