"""机台二维码标签（静态二维码），100×70mm @300DPI 热敏。

为什么是静态码、为什么单独一份文件而不是复用订单标签：
  - 机台码贴在机器上，一辈子不换，所以内容是固定的
    {"type":"machine","machineId":"MC0001"}；员工码那种 60 秒轮换的哈希
    在这里没有意义（机器不会掏手机）。
  - 版面和订单码完全不同：订单码是六行表格的信息卡，机台码只需要「一眼认出
    是哪台机器 + 一个够大的码」。共用一份模板会互相拖累，所以各写各的，
    但字体加载、颜色、DPI 从 order_label 拿，避免两处漂移。

一处硬约束：二维码里的 type 字段不能省。小程序扫到码要先本地判断这是机台码
还是订单码（订单码是 {"workOrderNo":...}），没有 type 就只能把机台码丢给订单
接口试错，用户会看到「找不到订单」这种驴唇不对马嘴的提示。
"""
import io
import json
from dataclasses import dataclass

import qrcode
from PIL import Image, ImageDraw

from app.order_label import _font

DPI = 300

# 100×70mm
LABEL_W = round(100 / 25.4 * DPI)  # 1181
LABEL_H = round(70 / 25.4 * DPI)   # 827
MARGIN = round(2 / 25.4 * DPI)     # 2mm 静区，热敏头边缘打不到

BLACK = (26, 26, 26)
GRAY = (120, 120, 120)
TEAL = (21, 112, 137)
PURPLE = (74, 26, 94)
BORDER = (120, 120, 120)
WHITE = (255, 255, 255)
RED = (200, 40, 40)


@dataclass
class MachineLabelData:
    code: str
    name: str = ""
    model: str = ""
    active: bool = True


def machine_qr_content(code: str) -> str:
    """机台码内容。与小程序端的 parseMachineCode 一一对应，改这里必须同步改那边。"""
    return json.dumps({"type": "machine", "machineId": code}, ensure_ascii=False)


def machine_qr_image(code: str, side: int) -> Image.Image:
    """按目标边长画二维码，模块宽度取整。

    不能先用 qrcode 出图再 resize：非整数倍缩放会让模块出现 3px/4px 混宽，
    便宜的手机摄像头和扫描枪都会挑片（和物料条码那次踩的是同一个坑）。
    这里拿模块矩阵自己画，边长 = 模块数 × 整数倍，宁可差几像素也不失真。
    """
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=1, border=2)
    qr.add_data(machine_qr_content(code))
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    box = max(1, side // n)
    size = n * box

    img = Image.new("RGB", (size, size), WHITE)
    draw = ImageDraw.Draw(img)
    ink = (0, 21, 41)
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if on:
                draw.rectangle([x * box, y * box, x * box + box - 1, y * box + box - 1], fill=ink)
    return img


def _text(draw, xy, s, size, color, bold=False, anchor="la"):
    # 热敏纸分辨率低，粗体用描边模拟，比换字体文件可靠（与 order_label 同法）
    draw.text(xy, s, font=_font(size), fill=color, anchor=anchor,
              stroke_width=2 if bold else 0, stroke_fill=color if bold else None)


def render_machine_label(data: MachineLabelData) -> bytes:
    img = Image.new("RGB", (LABEL_W, LABEL_H), WHITE)
    draw = ImageDraw.Draw(img)

    x0, y0 = MARGIN, MARGIN
    x1, y1 = LABEL_W - MARGIN, LABEL_H - MARGIN
    ih = y1 - y0

    # 左栏放码、右栏放字。左栏宽度按「二维码够大且不挤」定 0.40；
    # 420px ≈ 35.5mm 见方，手机隔三四十厘米就能扫上
    left_w = round((x1 - x0) * 0.40)
    split_x = x0 + left_w

    draw.rectangle([x0, y0, x1, y1], outline=BORDER, width=3)
    draw.line([(split_x, y0), (split_x, y1)], fill=BORDER, width=2)

    # ---- 左栏：二维码 + 底部提示 ----
    pad = 22
    caption_top = y1 - 70  # 提示行的上沿，二维码不得越过它
    side = min(left_w - pad * 2, caption_top - y0 - pad * 2)
    qr = machine_qr_image(data.code, side)
    qr_x = x0 + (left_w - side) // 2
    # 在「上边框」到「提示行上沿」之间垂直居中，否则码贴顶、下面空一大块
    qr_y = y0 + pad + (caption_top - y0 - pad * 2 - side) // 2
    img.paste(qr, (qr_x, qr_y))
    _text(draw, (x0 + left_w // 2, y1 - 46), "扫码关联开工单", 30, GRAY, anchor="mm")

    # ---- 右栏：编号 / 名称 / 型号 三行 ----
    pad_r = 30
    tx = split_x + pad_r
    right_w = x1 - pad_r - tx

    fr = [0.34, 0.33, 0.33]
    ys = [y0]
    for f in fr:
        ys.append(ys[-1] + round(f * ih))
    ys[-1] = y1
    for y in ys[1:-1]:
        draw.line([(split_x, y), (x1, y)], fill=BORDER, width=2)

    def cell(idx, label, value, size, color, bold=False):
        top, bot = ys[idx], ys[idx + 1]
        _text(draw, (tx, top + 16), label, 28, GRAY)
        _text(draw, (tx, bot - 26), _clip(draw, value, size, right_w), size, color,
              bold=bold, anchor="ls")

    cell(0, "机台编号", data.code or "—", 92, TEAL, bold=True)
    cell(1, "机台名称", data.name or "—", 54, BLACK)
    cell(2, "型    号", data.model or "—", 54, BLACK)

    # 停用机台照打（人可能先打一批备着），但必须一眼看出来，否则贴上去才发现
    # 扫不了。这里用右上角红字，不占正文位置
    if not data.active:
        _text(draw, (x1 - pad_r, y0 + 14), "已停用", 34, RED, bold=True, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _clip(draw, text: str, size: int, max_w: int) -> str:
    """超长名称截断加省略号，避免压到右边界（热敏纸压边会被切掉）。"""
    if not text:
        return "—"
    font = _font(size)
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    out = text
    while out and draw.textlength(out + ell, font=font) > max_w:
        out = out[:-1]
    return (out + ell) if out else ell
