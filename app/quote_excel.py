"""生成报价单 xlsx。

版式与 web 端 erp-mes/src/utils/exportQuote.js **逐项对齐**：11 列表头、列宽、
明细行、合计行（A:E 合并）、工作表名「报价单」、文件名「报价单-{id}.xlsx」。
这样小程序导出的文件和 web 端导出的可以互换，再用「导入报价」读回来也不会多出行
（合计行没有品名，parseQuote 会当无效行跳过）。

为什么挪到后端：原先走小程序 web-view 里的 H5，用 blob + <a download> 下载 ——
微信内置浏览器直接禁掉 blob 下载；跳到系统浏览器后，iOS Safari 对 blob 的
download 属性支持也是坏的（WebKit #190351，文件名会变成 Unknown、扩展名丢失）。
改成后端出真实附件，小程序用 downloadFile + openDocument 原生打开，两个坑一起绕开，
而且不再依赖 web-view 的业务域名配置。
"""

import io
import math

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

HEADERS = [
    "序号", "物料类别", "物料号", "品名", "规格/型号",
    "用量", "单位", "品牌/材质", "单价", "总价", "交期",
]
COL_WIDTHS = [6, 16, 12, 22, 32, 8, 8, 14, 10, 12, 14]
SHEET_NAME = "报价单"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# COL_WIDTHS 是「字符数」（对应 SheetJS 的 wch）。SheetJS 把它写进 xlsx 的
# <col width> 时会加 213/256 = 0.83203125 的字符内边距，这里照抄同一个偏移，
# 两端导出的列宽才能逐位一致（实测 JS 产物存的就是 6.83203125 这种值）。
_WCH_PADDING = 213 / 256

# JS 的 Number.EPSILON，round2 要逐位对齐就得用它
_JS_EPSILON = 2.220446049250313e-16


class QuoteExportError(Exception):
    """生成失败。message 会直接回给前端展示。"""


def quote_filename(quote_id: str) -> str:
    return f"报价单-{quote_id}.xlsx"


def round2(value: float) -> float:
    """与 exportQuote.js 的 round2 完全一致：Math.round((v + EPSILON) * 100) / 100。

    不用内置 round() —— 它是银行家舍入，2.675 会舍成 2.67 而 JS 得 2.68，
    两端导出的总价就会差一分钱。
    """
    return math.floor((float(value) + _JS_EPSILON) * 100 + 0.5) / 100


def build_quote_workbook(quote) -> Workbook:
    items = list(quote.items or [])
    if not items:
        raise QuoteExportError("该报价记录单没有明细，无法生成")

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(HEADERS)

    total_qty = 0
    total_amount = 0.0
    for idx, it in enumerate(items):
        quantity = int(it.quantity or 0)
        # 未填报价的明细，单价与总价都留空（不是 0）—— 0 会被误读成「免费」
        price = float(it.price) if it.price is not None else None
        total = round2(price * quantity) if price is not None else None
        total_qty += quantity
        if total is not None:
            total_amount += total

        ws.append([
            it.seq or idx + 1,
            it.category or "",
            it.material_no or "",
            it.product or "",
            it.model or "",
            quantity,
            it.unit or "",
            it.brand or "",
            price,
            total,
            it.delivery or "",
        ])

    # 合计行：A:E 合并成一格放「合计」，F 是用量合计，J 是总价合计
    total_row = len(items) + 2
    ws.append(["合计", "", "", "", "", total_qty, "", "", "", round2(total_amount), ""])
    ws.merge_cells(start_row=total_row, start_column=1, end_row=total_row, end_column=5)

    for i, width in enumerate(COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width + _WCH_PADDING

    return wb


def build_quote_xlsx(quote) -> bytes:
    buf = io.BytesIO()
    build_quote_workbook(quote).save(buf)
    return buf.getvalue()
