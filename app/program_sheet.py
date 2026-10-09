"""解析 CAM 软件导出的「钢料设定单」图纸（老式二进制 .xls），算出程序时间。

模板结构（星宫信息技术有限公司钢料设定单）：
    第 1~12 行  工件信息（客户名称、工件名称、工件尺寸、总时间(分)…）
    第 16 行    工序表头：程 序 名 称 | 刀具名称 | 刀号 | … | 时间 | 备注
    第 17 行起  每行一道工序，「时间」列是该工序的机加工分钟数

程序时间 = 「时间」列累加。实测模板 A1~A7 七道工序累加 = 167.4 分，
与表格自带的「总时间(分) 167.4」一致。

表头所在行和「时间」列的索引都**不能写死**：不同工件表头上方的信息行数可能不同，
列也可能被增删（模板里「时间」右边还跟着备注、新代机床、精光等列），所以一律按
单元格内容定位。
"""

from dataclasses import dataclass, field

import xlrd

TIME_HEADER = "时间"
# 工序表头一定在文件靠上的位置，往下扫这么多行足够，避免大表全表扫描
MAX_HEADER_SCAN_ROWS = 60
# 汇总行的名字里带这些词就跳过，否则会把「合计」行也累加进去变成双倍
TOTAL_ROW_MARKERS = ("合计", "总计", "总时间", "小计")


class ProgramSheetError(Exception):
    """图纸解析失败。message 会直接回给现场员工看，所以要写人话。"""


@dataclass
class ProgramSheetRow:
    name: str
    minutes: float


@dataclass
class ProgramSheetResult:
    total_minutes: float
    rows: list[ProgramSheetRow] = field(default_factory=list)
    sheet_name: str = ""


def parse_program_sheet(data: bytes) -> ProgramSheetResult:
    """解析图纸字节流，返回累加出的程序时间（分钟）和逐工序明细。"""
    book = _open_book(data)

    for sheet in book.sheets():
        header_row = _find_header_row(sheet)
        if header_row is None:
            continue
        time_col = _find_time_column(sheet, header_row)
        rows = _collect_time_rows(sheet, header_row, time_col)
        if not rows:
            raise ProgramSheetError(
                f"工作表「{sheet.name}」的「时间」列没有可累加的数值，"
                f"请确认图纸里已经填了各工序时间"
            )
        return ProgramSheetResult(
            total_minutes=round(sum(r.minutes for r in rows), 2),
            rows=rows,
            sheet_name=sheet.name,
        )

    raise ProgramSheetError(
        "图纸格式不符：找不到工序表头（同一行里应同时有「程序名称」和「时间」），"
        "请确认上传的是钢料设定单"
    )


def _open_book(data: bytes) -> xlrd.Book:
    if not data:
        raise ProgramSheetError("上传的文件是空的")
    try:
        return xlrd.open_workbook(file_contents=data)
    except Exception as e:  # xlrd 对各种坏文件抛的异常类型不统一，这里统一转成人话
        msg = str(e)
        # .xlsx 本质是 zip 容器。xlrd 对真 xlsx 报「Excel xlsx file; not supported」，
        # 对其它 zip 报「Unknown ZIP file; not supported」，两种都归到「你传错格式了」。
        low = msg.lower()
        if "xlsx" in low or "zip" in low:
            raise ProgramSheetError(
                "这是 .xlsx（或其它 zip 格式）文件，目前只支持老式 .xls；"
                "请用 Excel「另存为」选 .xls 后再上传"
            ) from e
        raise ProgramSheetError(f"图纸文件打不开，请确认是 .xls 格式（{msg}）") from e


def _cell_text(sheet, row: int, col: int) -> str:
    return str(sheet.cell_value(row, col)).strip()


def _find_header_row(sheet) -> int | None:
    """工序表头行：同一行里既有「时间」，又有一个含「程序」的列名。

    模板里那个列名写作「程 序 名 称」（字之间带空格），所以比对前先去掉空格。
    """
    for r in range(min(sheet.nrows, MAX_HEADER_SCAN_ROWS)):
        texts = [_cell_text(sheet, r, c) for c in range(sheet.ncols)]
        if TIME_HEADER in texts and any("程序" in t.replace(" ", "") for t in texts):
            return r
    return None


def _find_time_column(sheet, header_row: int) -> int:
    for c in range(sheet.ncols):
        if _cell_text(sheet, header_row, c) == TIME_HEADER:
            return c
    # 走不到这里：_find_header_row 已经确认过该行有「时间」
    raise ProgramSheetError("表头里找不到「时间」列")


def _collect_time_rows(sheet, header_row: int, time_col: int) -> list[ProgramSheetRow]:
    rows: list[ProgramSheetRow] = []
    for r in range(header_row + 1, sheet.nrows):
        name = _cell_text(sheet, r, 0)
        if any(marker in name for marker in TOTAL_ROW_MARKERS):
            continue
        minutes = _as_minutes(sheet.cell(r, time_col))
        if minutes is None:
            continue
        rows.append(ProgramSheetRow(name=name, minutes=minutes))
    return rows


def _as_minutes(cell) -> float | None:
    """只收数值。文本形式的数字也认（现场有人把单元格格式设成文本），其余忽略。"""
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        return float(cell.value)
    if cell.ctype == xlrd.XL_CELL_TEXT:
        text = cell.value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None
