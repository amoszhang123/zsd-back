"""客户月度对账单渲染。

和送货单是两套东西，所以单独成文件：送货单是 7 列窄表、按张打；对账单是客户
Excel 模板的 10 列宽表、按月把多张送货单拼在一起，还要算合计和大写金额。塞进
同一个函数会让两边都改不动。

版式对照 `展晟达泰松10月对账明细.xlsx`：
  三行抬头（客户名 / 期间 / 供应商名称）+ 10 列表头 + 明细 + 两行合计 + 两处签字。
抬头塞进 <thead>，Chromium 会在每页顶部重放 —— 356 行的月份会打 9~10 页，
散页后第 5 页得能看出是谁家的账。

合计行**故意不放 <tfoot>**：Chrome 的 tfoot 和 thead 一样每页重复，会把
「总金额 223514」印在每一页底部，财务翻页时会当成 10 个 22 万。
"""

from __future__ import annotations

import json
from html import escape

# 10 列的宽度（%），对应下面的 9pt（小五）字号。
# 按「值不折行、表头允许折两行」反推。注意**要按 Windows 宋体算**：宋体的数字/字母
# 是 0.5em（9pt 下 6px/字符），而 Mac 上的回退字体只有 0.469em，按 Mac 量出来的
# 宽度搬到车间会窄 6%，末位那个「B」就掉到第二行（送货单踩过一次）。
# 各项内容宽（9pt，拉丁 6px/字符、汉字 12px）：
#   序号 24(表头) / 物料号 48(8 位) / 产品名称 108(9 汉字) / 型号规格 174(29 字符)
#   品牌 60(5 汉字) / 数量 36(「（个）」) / 含税单价 48 / 含税总价 48
#   备注 66(10 位单号) / 项目号 66(11 字符)
# 版心 = A4 210mm - 左右 6mm = 198mm ≈ 748px；每格另耗 2px 边框 + 4px 内边距。
# 为什么是 9pt 而不是模板的 11pt：模板列宽合计 134 字符，A4 纵向打印本来就得
# 「缩放到一页宽」（实测约 72%），纸上真实效果不到 9pt。直接排 9pt 才是模板的
# 打印效果，而且不用让浏览器替我们缩。
# 改列宽前先量最长型号：SELECT MAX(CHAR_LENGTH(model)) FROM orders
COLUMNS = [
    ("序号", 4.1),
    ("物料号", 7.3),
    ("产品名称", 15.3),
    ("型号规格", 24.7),
    ("品牌", 8.9),
    ("数量（个）", 5.7),
    ("含税单价（元）", 7.3),
    ("含税总价（元）", 7.3),
    # 换行符是受控折行：不写的话「备注（订单号）」会断成「备注（订 / 单号）」
    ("备注\n（订单号）", 9.2),
    ("项目号", 10.2),
]

_CN_DIGIT = "零壹贰叁肆伍陆柒捌玖"
_CN_POS = ("", "拾", "佰", "仟")
_CN_GROUP = ("", "万", "亿", "万亿")


def _cn_group(value: int) -> str:
    """一组（4 位以内）的大写读法，组内零只在中间补一次：105 → 壹佰零伍。"""
    out = ""
    gap = False
    for pos in range(3, -1, -1):
        digit = (value // 10**pos) % 10
        if digit == 0:
            if out:  # 组内前面已有数字，才需要补零
                gap = True
            continue
        if gap:
            out += "零"
            gap = False
        out += _CN_DIGIT[digit] + _CN_POS[pos]
    return out


def _cn_int(value: int) -> str:
    """整数转中文大写。223514 → 贰拾贰万叁仟伍佰壹拾肆。

    按 4 位一组读，不逐位读。逐位读会在 100001 上出错：中间那 4 个 0 会先补出
    一个「零」再加「万」，读成「壹拾零万零壹」。分组后「万」永远跟着「壹拾」走，
    零只在「低位组不足千」或「整组为零」时补一个。
    """
    if value == 0:
        return "零"
    groups: list[int] = []
    rest = value
    while rest:
        groups.append(rest % 10000)
        rest //= 10000
    out = ""
    need_zero = False
    for idx in range(len(groups) - 1, -1, -1):
        group = groups[idx]
        if group == 0:
            need_zero = True  # 整组为零：等下一个非零组前面补一个零
            continue
        if out and (need_zero or group < 1000):
            out += "零"
        out += _cn_group(group) + _CN_GROUP[idx]
        need_zero = False
    return out


def cn_amount(amount: float) -> str:
    """金额转人民币大写：223514 → 贰拾贰万叁仟伍佰壹拾肆圆整。

    先换成「分」再拆，避免 0.1+0.2 这类浮点尾数把分位读错。
    """
    cents = int(round(abs(float(amount or 0)) * 100))
    if cents == 0:
        return "零圆整"
    yuan, jiao, fen = cents // 100, cents // 10 % 10, cents % 10
    text = _cn_int(yuan) + "圆" if yuan else ""
    if not jiao and not fen:
        return text + "整"
    if jiao:
        text += _CN_DIGIT[jiao] + "角"
    elif yuan and fen:
        text += "零"  # 有元无角有分：壹佰零伍分，不能读成壹佰伍分
    if fen:
        text += _CN_DIGIT[fen] + "分"
    return text


STATEMENT_CSS = """
  /* A4 纵向，左右 6mm：10 列 + 29 字符型号，纵向只有这么压才排得下一行 */
  @page { size: A4; margin: 12mm 6mm; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: "SimSun", "宋体", "Songti SC", serif; font-size: 9pt; color: #000; }
  /* 纸张留白只由 @page 负责；这里再加 padding 会让纸上边距变成 6mm+固定像素 */
  .sheet { page-break-after: always; break-after: page; }
  .sheet:last-child { page-break-after: auto; break-after: auto; }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  /* fixed：百分比是铁律，长型号不许把列撑宽导致整表顶出版心 */
  th, td { border: 1px solid #000; padding: 2px 2px; text-align: center;
           font-size: 9pt; line-height: 1.35; }
  /* 抬头三行随表头一起每页重复 */
  thead tr.t-cust th { font-size: 14pt; font-weight: 700; border: 0; padding: 4px 0 2px; }
  thead tr.t-period th { font-size: 11pt; font-weight: 700; border: 0; padding: 0 0 2px; }
  thead tr.t-supplier th { font-size: 10pt; border: 0; padding: 0 0 6px; }
  thead tr.t-head th { background: #f2f2f2; font-weight: 700; }
  /* 短字段恒定一行；产品/型号/品牌/项目号超长时只让那一格折行，不压到隔壁格 */
  td.nw { white-space: nowrap; }
  td.num { text-align: right; padding-right: 3px; white-space: nowrap; }
  td.sum-label { font-weight: 700; text-align: center; }
  td.sum-value { text-align: right; padding-right: 5px; white-space: nowrap; }
  .sign { display: flex; justify-content: space-between; margin-top: 22px;
          font-size: 11pt; }
  .sign div { width: 45%; }
  /* 工具条只给屏幕看：纸上不该多出一条给客户看的操作说明 */
  .bar { position: sticky; top: 0; z-index: 9; display: flex; flex-wrap: wrap;
         gap: 10px; align-items: center; background: #fff8e1;
         border-bottom: 1px solid #f0c36d; color: #8a5b00; padding: 8px 12px;
         font-family: "Microsoft YaHei", "PingFang SC", sans-serif; font-size: 13px; }
  .bar select, .bar input { font: inherit; padding: 3px 6px; border: 1px solid #d0a750;
                            border-radius: 3px; background: #fff; color: #333; }
  .bar input { width: 320px; }
  .bar button { font: inherit; font-weight: 600; padding: 4px 14px; cursor: pointer;
                border: 1px solid #d0a750; border-radius: 3px;
                background: #fff; color: #8a5b00; }
  .bar .tip { font-size: 12px; color: #a07020; }
  .empty { padding: 40px 0; text-align: center; font-size: 12pt; }
  @media screen {
    body { background: #eef1f5; padding-bottom: 16px; }
    .sheet { background: #fff; width: 210mm; padding: 12mm 6mm; margin: 0 auto;
             box-shadow: 0 1px 8px rgba(0, 0, 0, .16); }
  }
  @media print { .bar { display: none; } }
"""


def _colgroup() -> str:
    cols = "".join(f'<col style="width:{w}%">' for _, w in COLUMNS)
    return f"<colgroup>{cols}</colgroup>"


def _head_head(customer: str, period: str, supplier: str) -> str:
    """表头四行：客户名 / 期间 / 供应商 / 列名。整块放进 thead，每页自动重放。"""
    label = "".join(
        # 列名里的换行符转成 <br>：让「备注 /（订单号）」这类长表头按我们指定的
        # 位置断行，而不是交给浏览器断在「订」和「单号」之间
        f"<th>{'<br>'.join(escape(part) for part in name.split(chr(10)))}</th>"
        for name, _ in COLUMNS
    )
    span = len(COLUMNS)
    return (
        "<thead>"
        f'<tr class="t-cust"><th colspan="{span}">{escape(customer)}</th></tr>'
        f'<tr class="t-period"><th colspan="{span}">{escape(period)}对账单</th></tr>'
        f'<tr class="t-supplier"><th colspan="{span}">供应商名称：{escape(supplier)}</th></tr>'
        f'<tr class="t-head">{label}</tr>'
        "</thead>"
    )


def _money(value: float, sep: bool = False) -> str:
    """金额打印格式：去掉无意义的小数位，跟模板一致（10710、165.6 而不是 10710.00）。

    不只是好看：一列只有 48px 宽，「1481400.00」比「1481400」宽 12px，正好把
    「含税总价」挤到折行。合计行加千分位，几百万的数不加分隔符没法一眼读。
    """
    text = f"{float(value or 0):,.2f}" if sep else f"{float(value or 0):.2f}"
    return text.rstrip("0").rstrip(".")


def _rows_html(rows: list[dict]) -> str:
    out = []
    for i, r in enumerate(rows, 1):
        out.append(
            "<tr>"
            f"<td class='nw'>{i}</td>"
            f"<td class='nw'>{escape(str(r['material_no']))}</td>"
            f"<td>{escape(str(r['product']))}</td>"
            f"<td class='nw'>{escape(str(r['model']))}</td>"
            f"<td>{escape(str(r['brand']))}</td>"
            # 数量居中（模板也是居中）：右对齐会和紧挨着的「含税单价」粘成一串数字
            f"<td>{r['quantity']}</td>"
            f"<td class='num'>{_money(r['unit_price'])}</td>"
            f"<td class='num'>{_money(r['amount'])}</td>"
            f"<td class='nw'>{escape(str(r['purchase_no']))}</td>"
            f"<td>{escape(str(r['project_no']))}</td>"
            "</tr>"
        )
    return "".join(out)


def _sum_html(total: float) -> str:
    """合计两行：前 5 列留空，标签占 2 列、数值占 3 列。

    数值给 3 列（约 191px）是为了让大写金额「贰拾贰万叁仟伍佰壹拾肆圆整」不折行 ——
    折了虽然不算错，但财务会怀疑是不是两个数。
    这是 tbody 的末尾行，不是 tfoot —— 见模块开头的说明。
    """
    return (
        '<tr class="sum-row">'
        '<td colspan="5" style="border:0"></td>'
        '<td colspan="2" class="sum-label">总金额（小写）</td>'
        f'<td colspan="3" class="sum-value">{_money(total, sep=True)}</td>'
        "</tr>"
        '<tr class="sum-row">'
        '<td colspan="5" style="border:0"></td>'
        '<td colspan="2" class="sum-label">大写：</td>'
        f'<td colspan="3" class="sum-value">{cn_amount(total)}</td>'
        "</tr>"
    )


def _bar(scope: str, month_label: str, single_label: str, title: str) -> str:
    """屏幕工具条：切范围 / 改文件名 / 打印。打印时整条隐藏。

    改文件名只动 <title>（决定打印预览标题和「另存为 PDF」的默认名），纸上抬头
    固定是三行，不受影响 —— 所以输入框旁边要写明，否则会以为改的是纸上文字。
    """
    month_sel = " selected" if scope != "single" else ""
    single_sel = " selected" if scope == "single" else ""
    # </script> 会把内联脚本提前闭合：文件名是 URL 参数，必须转义掉
    title_js = json.dumps(title, ensure_ascii=False).replace("</", "<\\/")
    return f"""<div class="bar">
  <label>对账范围
    <select onchange="switchScope(this.value)">
      <option value="month"{month_sel}>{escape(month_label)}</option>
      <option value="single"{single_sel}>{escape(single_label)}</option>
    </select>
  </label>
  <label>文件名
    <input id="fn" value="{escape(title)}"
           oninput="renameStatement(this.value)"
           title="只影响打印预览标题和另存 PDF 的默认文件名，纸上的抬头不跟着变">
  </label>
  <button onclick="window.print()">&#128424; 打印</button>
  <span class="tip">改的是「另存为 PDF」的文件名，纸面抬头不变</span>
</div>
<script>
  var DEFAULT_TITLE = {title_js};
  function switchScope(v) {{
    var u = new URL(location.href);
    u.searchParams.set('scope', v);
    // 换范围后文件名回到该范围的默认名，免得「本单」的标题挂到整月的账上
    u.searchParams.delete('title');
    location.href = u.toString();
  }}
  function renameStatement(v) {{
    document.title = v || DEFAULT_TITLE;
    var u = new URL(location.href);
    if (v) u.searchParams.set('title', v); else u.searchParams.delete('title');
    history.replaceState(null, '', u.toString());
  }}
</script>"""


def render_statement(
    *,
    customer: str,
    supplier: str,
    period: str,
    rows: list[dict],
    total: float,
    title: str,
    scope: str,
    month_label: str,
    single_label: str,
    empty_hint: str = "",
) -> str:
    body = (
        f"<table>{_colgroup()}{_head_head(customer, period, supplier)}"
        f"<tbody>{_rows_html(rows)}{_sum_html(total) if rows else ''}</tbody></table>"
        '<div class="sign"><div>供应商签字盖章：</div><div>付款方签字盖章：</div></div>'
    )
    if not rows:
        body = f'<div class="empty">{escape(empty_hint)}</div>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{escape(title)}</title>
<style>{STATEMENT_CSS}</style>
</head>
<body>
{_bar(scope, month_label, single_label, title)}
<div class="sheet">{body}</div>
</body>
</html>"""
