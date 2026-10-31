"""物料条码标签（Code128 一维码），50×30mm @300DPI。

为什么用一维码而不是复用项目里已有的 qrcode：
  - 料盒/料架位窄，50×30 的小标签上二维码至少要占 15mm 见方才扫得稳，一维码
    只占 13mm 高就能读；
  - 现场那批扫码枪本身就是一维枪，读 Code128 是最稳的模式；
  - 物料码是定长流水号 WL000001，一维码足够，不需要二维码的信息容量。

单个标签走 /api/materials/{id}/label，批量走 /api/materials/labels 出 A4 拼版
（4 列 × 9 行 = 36 张/页），首次建账贴几十个料架时不用一张张点。

字体复用 order_label 的加载逻辑：中文字体在 macOS 上要挑候选路径，
两处各写一份候选表迟早会漂。
"""
import io
from dataclasses import dataclass

from barcode import Code128
from PIL import Image, ImageDraw

from app.order_label import _font

DPI = 300

# 50×30mm
LABEL_W = round(50 / 25.4 * DPI)   # 591
LABEL_H = round(30 / 25.4 * DPI)   # 354
PAD = 14

# A4 拼版：4 列 × 9 行
PAGE_W = round(210 / 25.4 * DPI)   # 2480
PAGE_H = round(297 / 25.4 * DPI)   # 3508
COLS, ROWS = 4, 9
PER_PAGE = COLS * ROWS

BLACK = (17, 17, 17)
GRAY = (110, 110, 110)
BORDER = (150, 150, 150)
WHITE = (255, 255, 255)

# 版面纵向分区（px）
Y_NAME = 10
Y_SPEC = 46
Y_BAR_TOP = 74
Y_BAR_BOT = 226
Y_CODE = 236
Y_FOOTER = 306


@dataclass
class MaterialLabelData:
    code: str
    name: str
    spec: str = ""
    unit: str = ""
    location: str = ""


def _fit(draw, text: str, size: int, max_w: int) -> tuple[str, object]:
    """超宽先缩字号，缩到下限仍放不下就截断加省略号。"""
    font = _font(size)
    while size > 14 and draw.textlength(text, font=font) > max_w:
        size -= 2
        font = _font(size)
    if draw.textlength(text, font=font) > max_w:
        while len(text) > 1 and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        text += "…"
    return text, font


def _barcode_image(code: str, target_w: int, target_h: int) -> Image.Image:
    """自己按模块画条，不走 ImageWriter。

    ImageWriter 的 module_width 是毫米、落图时逐模块取整，101 个模块里会出现
    「4px/5px 混着」的宽度差，便宜的一维枪读这种码会挑片。这里直接拿
    Code128 的 0/1 模式，指定整数像素/模块，条宽绝对均匀。
    """
    pattern = "".join(Code128(code).build())
    modules = len(pattern)
    # 每模块像素数取整，最多 6px（再宽就只是变大，不影响识别率）
    module_px = max(1, min(6, target_w // max(modules, 1)))
    bar_h = max(24, target_h)

    img = Image.new("1", (modules * module_px, bar_h), 1)
    draw = ImageDraw.Draw(img)
    i = 0
    while i < modules:
        if pattern[i] == "1":
            j = i
            while j < modules and pattern[j] == "1":
                j += 1
            draw.rectangle(
                [i * module_px, 0, j * module_px - 1, bar_h - 1], fill=0
            )
            i = j
        else:
            i += 1
    return img.convert("L")


def render_material_label(data: MaterialLabelData) -> Image.Image:
    img = Image.new("RGB", (LABEL_W, LABEL_H), WHITE)
    draw = ImageDraw.Draw(img)
    inner_w = LABEL_W - PAD * 2

    draw.rectangle([2, 2, LABEL_W - 3, LABEL_H - 3], outline=BORDER, width=2)

    # 名称占主视觉，规格退一档灰字 —— 拣料时先看名字，再核规格
    name, font = _fit(draw, data.name or data.code, 30, inner_w)
    draw.text((PAD, Y_NAME), name, font=font, fill=BLACK)
    if data.spec:
        spec, sfont = _fit(draw, data.spec, 20, inner_w)
        draw.text((PAD, Y_SPEC), spec, font=sfont, fill=GRAY)

    # 条码左右各留 6px 静区，否则贴边会扫不出
    bar_w = inner_w - 12
    bar = _barcode_image(data.code, bar_w, Y_BAR_BOT - Y_BAR_TOP)
    img.paste(bar, (PAD + 6 + (bar_w - bar.width) // 2, Y_BAR_TOP +
                    ((Y_BAR_BOT - Y_BAR_TOP) - bar.height) // 2))

    # 明文编码居中，和条码内容一字不差（坏了能手输）
    cfont = _font(36)
    cw = draw.textlength(data.code, font=cfont)
    draw.text(((LABEL_W - cw) / 2, Y_CODE), data.code, font=cfont, fill=BLACK)

    # 页脚：库位 + 单位。库位是「补货/归位去哪」的唯一线索，必须有
    foot_left = f"库位 {data.location}" if data.location else "库位 —"
    foot, ffont = _fit(draw, foot_left, 20, inner_w - 70)
    draw.text((PAD, Y_FOOTER), foot, font=ffont, fill=GRAY)
    if data.unit:
        uw = draw.textlength(data.unit, font=ffont)
        draw.text((LABEL_W - PAD - uw, Y_FOOTER), data.unit, font=ffont, fill=GRAY)

    return img


def render_label_sheet(items: list[MaterialLabelData]) -> list[bytes]:
    """把标签按 4×9 拼到 A4 页，返回每页 PNG 字节。"""
    pages: list[bytes] = []
    for start in range(0, len(items), PER_PAGE):
        chunk = items[start:start + PER_PAGE]
        page = Image.new("RGB", (PAGE_W, PAGE_H), WHITE)
        ox = (PAGE_W - COLS * LABEL_W) // 2
        oy = (PAGE_H - ROWS * LABEL_H) // 2
        for i, data in enumerate(chunk):
            col, row = i % COLS, i // COLS
            page.paste(render_material_label(data), (ox + col * LABEL_W, oy + row * LABEL_H))
        buf = io.BytesIO()
        page.save(buf, format="PNG")
        pages.append(buf.getvalue())
    return pages


def label_png(data: MaterialLabelData) -> bytes:
    buf = io.BytesIO()
    render_material_label(data).save(buf, format="PNG")
    return buf.getvalue()
