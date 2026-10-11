"""订单码标签渲染（佳博热敏条码打印机用）。

版式照 /Users/amos.zhang/Downloads/code.jpg 干净重绘，而不是往那张照片上叠字：
原图是手机拍的屏幕，带摩尔纹、反光和 Excel 标签页，直接当底图会把瑕疵一起
打印到热敏纸上。这里只借用它的版面与配色。

版面（100×70mm，300 DPI）：
    ┌────────┬────────────────────────────┬────────┐
    │  ZSD   │ 订单号：xxx                │   121  │  ← 右上角=合同序号
    │ 展晟达 ├────────────────────────────┤        │
    │        │ 项目号：xxx                │        │
    ├────────┴────────────────────────────┤        │
    │ 型    号：xxx                        │        │
    ├──────────────────────────────┬──────┤        │
    │ 物料编码：xxx                │ ▓▓▓  │  ← 二维码跨物料编码/名称两行
    ├──────────────────────────────┤ ▓▓▓  │
    │ 物料名称：xxx                │ ▓▓▓  │
    ├──────────────────────────────┴──────┤        │
    │ 数量：3 pcs                9月22日  │  ← 右下角=交期
    └─────────────────────────────────────┴────────┘
"""
from dataclasses import dataclass

import qrcode
from PIL import Image, ImageDraw, ImageFont

# 100×70mm @ 300 DPI
DPI = 300
LABEL_W = round(100 / 25.4 * DPI)  # 1181
LABEL_H = round(70 / 25.4 * DPI)   # 827
MARGIN = round(2 / 25.4 * DPI)     # 2mm 静区，热敏头边缘打不到

# 从模板照片采样的感知色（照片偏暗，这里取屏幕上看到的颜色）。
# 热敏打印是单色，彩色只影响预览观感。
PURPLE = (74, 26, 94)     # 订单号值 / 右上角序号 / 右下角交期
TEAL = (21, 112, 137)     # 项目号值 / 数量值
BLACK = (26, 26, 26)      # 标签文字与黑白值
BORDER = (120, 120, 120)  # 表格线
WHITE = (255, 255, 255)

# 中文字体候选，按优先级取第一个存在的。macOS 上 STHeiti / Hiragino 都在。
_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
]

_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _font_cache:
        last_err = None
        for path in _FONT_CANDIDATES:
            try:
                _font_cache[size] = ImageFont.truetype(path, size)
                break
            except OSError as e:
                last_err = e
        else:
            raise RuntimeError(f"找不到可用中文字体，候选：{_FONT_CANDIDATES}") from last_err
    return _font_cache[size]


@dataclass
class LabelData:
    order_no: str
    project_no: str
    model: str
    material_no: str
    product: str
    quantity: int
    seq: str        # 合同明细序号，右上角大字
    deadline: str   # 交期，右下角大字，如「9月22日」


def order_qr_content(order) -> str:
    """订单二维码内容。web 端 /orders/{id}/qrcode 与标签共用这一份，避免两处漂移。"""
    import json
    return json.dumps({
        "workOrderNo": order.id,
        "orderNo": order.order_no,
        "projectNo": order.project_no,
    }, ensure_ascii=False)


def order_qr_image(order, box_size: int = 10, border: int = 2) -> Image.Image:
    qr = qrcode.QRCode(version=1, box_size=box_size, border=border)
    qr.add_data(order_qr_content(order))
    qr.make(fit=True)
    return qr.make_image(fill_color="#001529", back_color="white").convert("RGB")


def _text(draw, xy, s, size, color, bold=False, anchor="la"):
    # 热敏纸分辨率低，粗体用描边模拟，比换字体文件可靠
    draw.text(xy, s, font=_font(size), fill=color, anchor=anchor,
              stroke_width=2 if bold else 0, stroke_fill=color if bold else None)


def render_label(data: LabelData, qr: Image.Image) -> bytes:
    import io

    img = Image.new("RGB", (LABEL_W, LABEL_H), WHITE)
    draw = ImageDraw.Draw(img)

    x0, y0 = MARGIN, MARGIN
    x1, y1 = LABEL_W - MARGIN, LABEL_H - MARGIN
    iw, ih = x1 - x0, y1 - y0

    # 六行高度占比，合计 1.0
    fr = [0.17, 0.13, 0.16, 0.17, 0.17, 0.20]
    ys = [y0]
    for f in fr:
        ys.append(ys[-1] + round(f * ih))
    ys[-1] = y1

    logo_w = round(iw * 0.24)
    logo_x = x0 + logo_w

    pad = 18
    val = 46
    gap = 30

    # 二维码跨「物料编码 / 物料名称」两行贴右侧。位置要先算出来再画横线，
    # 否则 D/E 之间的横线会穿过二维码
    qr_side = (ys[5] - ys[3]) - pad * 2
    qr = qr.resize((qr_side, qr_side), Image.NEAREST)
    qr_x = x1 - pad - qr_side
    qr_y = ys[3] + pad
    val_right = qr_x - pad   # 物料编码/名称的值不得压到二维码

    # 表格线。logo 是合并单元格（ZSD/展晟达 之间无横线），所以 A/B 分隔线只画右半；
    # D/E 分隔线在二维码左侧截止
    draw.rectangle([x0, y0, x1, y1], outline=BORDER, width=3)
    draw.line([(logo_x, ys[1]), (x1, ys[1])], fill=BORDER, width=2)
    for y in (ys[2], ys[3], ys[5]):
        draw.line([(x0, y), (x1, y)], fill=BORDER, width=2)
    draw.line([(x0, ys[4]), (val_right, ys[4])], fill=BORDER, width=2)
    draw.line([(logo_x, y0), (logo_x, ys[2])], fill=BORDER, width=2)

    # logo 合并单元格：ZSD / 展晟达 在整格里上下居中
    cx = x0 + logo_w // 2
    mid_logo = (y0 + ys[2]) // 2
    _text(draw, (cx, mid_logo - 42), "ZSD", 54, BLACK, anchor="mm")
    _text(draw, (cx, mid_logo + 46), "展晟达", 50, BLACK, anchor="mm")

    # 右上角序号 / 右下角交期先量宽度，左边的值才知道要让到哪里。
    # 缺数据时留空而不是画「—」：92px 的破折号加描边看着像一根实心条，
    # 「没填交期 / 无合同序号」的提示改由前端预览弹窗负责。
    seq_w = _bold_width(draw, data.seq, 92)
    dl_w = _bold_width(draw, data.deadline, 76)
    if data.seq:
        _text(draw, (x1 - pad, (ys[0] + ys[1]) // 2), data.seq, 92, PURPLE,
              bold=True, anchor="rm")
    if data.deadline:
        _text(draw, (x1 - pad, (ys[5] + ys[6]) // 2), data.deadline, 76, PURPLE,
              bold=True, anchor="rm")

    # 行 A：订单号（logo 右侧起），值不得压到右上角序号
    _lv(draw, logo_x + pad, (ys[0] + ys[1]) // 2, "订单号：", data.order_no or "—",
        val + 4, PURPLE, True, gap, x1 - pad - seq_w - 24)

    # 行 B：项目号
    _lv(draw, logo_x + pad, (ys[1] + ys[2]) // 2, "项目号：", data.project_no or "—",
        val, TEAL, True, gap, x1 - pad)

    # 行 C：型号
    _lv(draw, x0 + pad, (ys[2] + ys[3]) // 2, "型    号：", data.model or "—",
        val, BLACK, False, gap, x1 - pad)

    # 行 D / E：物料编码、物料名称（值宽度让位给二维码）
    _lv(draw, x0 + pad, (ys[3] + ys[4]) // 2, "物料编码：", data.material_no or "—",
        val, BLACK, False, gap, val_right)
    _lv(draw, x0 + pad, (ys[4] + ys[5]) // 2, "物料名称：", data.product or "—",
        val, BLACK, False, gap, val_right)

    # 行 F：数量，值不得压到右下角交期
    _lv(draw, x0 + pad, (ys[5] + ys[6]) // 2, "数量：", f"{data.quantity} pcs",
        val, TEAL, True, gap, x1 - pad - dl_w - 24)

    img.paste(qr, (qr_x, qr_y))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _bold_width(draw, s, size):
    """带描边粗体的实际占宽。描边每侧约 2px，留 8px 余量；空串返回 0。"""
    return draw.textlength(s, font=_font(size)) + 8 if s else 0


def _lv(draw, x, ymid, label, value, vsize, vcolor, vbold, gap, right_limit=None):
    """画「标签：值」一行，值紧跟标签宽度排布；right_limit 给定时超宽自动缩字号。"""
    _text(draw, (x, ymid), label, 44, BLACK, anchor="lm")
    vx = x + draw.textlength(label, font=_font(44)) + gap
    if right_limit is not None:
        _draw_fit(draw, (vx, ymid), value, vsize, vcolor, right_limit, bold=vbold)
    else:
        _text(draw, (vx, ymid), value, vsize, vcolor, bold=vbold, anchor="lm")


def _draw_fit(draw, xy, s, size, color, right_limit, bold=False):
    """值太长先缩字号；缩到下限仍放不下就截断加省略号，宁可少字也不能画出标签外。"""
    x, y = xy
    avail = right_limit - x
    font = _font(size)
    while size > 24 and draw.textlength(s, font=font) > avail:
        size -= 2
        font = _font(size)
    if draw.textlength(s, font=font) > avail:
        while len(s) > 1 and draw.textlength(s + "…", font=font) > avail:
            s = s[:-1]
        s += "…"
    draw.text((x, y), s, font=font, fill=color, anchor="lm",
              stroke_width=2 if bold else 0, stroke_fill=color if bold else None)
