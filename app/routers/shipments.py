from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.routers.auth import _get_token
from app import audit
from app.database import get_db
from app.models.contract import Contract, ContractItem
from app.models.order import SHIPPING_STEP, Order, OrderStep, resolve_order_status
from app.models.production import in_production_order_ids
from app.models.quality import QcInspection
from app.models.shipment import (
    Shipment,
    ShipmentItem,
    fulfilled_order_ids,
    shipped_qty_map,
)
from app.schemas.shipment import (
    ContractOrderOut,
    ContractProgressOut,
    ShipmentCreate,
    ShipmentDateUpdate,
    ShipmentItemOut,
    ShipmentOut,
)
from app.statement import render_statement

router = APIRouter(prefix="/api/shipments", tags=["shipments"])

# 对账单是唯一一个「浏览器直接打开 URL」的接口（新标签页打印），
# <a>/window.open 发的请求带不了 Authorization 头，所以它单独放行，
# 改为接受 ?token= 查询参数。其余接口一律走统一鉴权（见 main.py）。
public_router = APIRouter(prefix="/api/shipments", tags=["shipments"])


def _require_print_token(raw: str) -> dict:
    """打印页鉴权：header 里的 Bearer 或 URL 上的 ?token= 都认。

    为什么不直接放进白名单：对账单上有客户名、项目号、金额，
    裸奔等于把客户台账公开。URL 里带 token 会被浏览器历史和反代日志记录，
    但它是一次性打开的打印页，权衡下来比开放整个接口可接受。
    """
    token = (raw or "").replace("Bearer ", "").strip()
    info = _get_token(token) if token else None
    if not info:
        raise HTTPException(401, "未登录或链接已过期，请重新从出货管理打开")
    return info

QC2_STAGE = "quality2"
QC2_VERDICT_PASS = "pass"
# 终检判定 → 中文，用于拒绝出货时的提示；"" 表示这张工单还没做过终检
QC2_VERDICT_LABELS = {
    "": "尚未终检",
    "pass": "合格",
    "rework": "返工",
    "scrap": "报废",
    "repair": "维修",
}
DOC_PREFIX = "ZSD"


def _qc2_summary(db: Session, order_ids: list[str]) -> dict[str, dict]:
    """订单 → 质检2 累计合格数与最近一次判定。出货完成度按质检2 各批次合格数之和计。"""
    out: dict[str, dict] = {}
    if not order_ids:
        return out
    batches = (
        db.query(QcInspection)
        .filter(QcInspection.order_id.in_(order_ids), QcInspection.stage == QC2_STAGE)
        .order_by(QcInspection.created_at, QcInspection.id)
        .all()
    )
    for b in batches:
        s = out.setdefault(b.order_id, {"pass": 0, "verdict": ""})
        s["pass"] += b.pass_count
        s["verdict"] = b.verdict
    return out


def _load_contract_rows(db: Session, contract: Contract):
    """[(合同明细, 工单|None, 质检2汇总)]，按项目号 + 序号排序。"""
    items = (
        db.query(ContractItem)
        .filter(ContractItem.contract_id == contract.id)
        .order_by(ContractItem.project_no, ContractItem.seq)
        .all()
    )
    order_ids = [i.order_id for i in items if i.order_id]

    orders: dict[str, Order] = {}
    qc2: dict[str, dict] = {}
    if order_ids:
        orders = {o.id: o for o in db.query(Order).filter(Order.id.in_(order_ids)).all()}
        qc2 = _qc2_summary(db, order_ids)

    return [(i, orders.get(i.order_id), qc2.get(i.order_id, {})) for i in items]


def _shipped_doc_map(db: Session, contract_id: str) -> dict[str, str]:
    rows = (
        db.query(ShipmentItem.order_id, Shipment.doc_no)
        .join(Shipment, Shipment.id == ShipmentItem.shipment_id)
        .filter(Shipment.contract_id == contract_id, ShipmentItem.order_id.isnot(None))
        .order_by(Shipment.id)
        .all()
    )
    return {order_id: doc_no for order_id, doc_no in rows}


def _next_doc_no(db: Session, day: date) -> str:
    """生成单号：ZSD + 出货日期(yyyymmdd) + 当天第几张。

    序号取「当天已有的最大序号 + 1」，并对该日的单号区间加锁 —— 两个人同时点出货
    时排队拿号，而不是各自数到同一个数再撞唯一索引（撞了就是 500，一张单都没生成）。
    两位序号用满 99 之后自然进位成三位（101），历史单不会被改成 001。

    参数 day 是**出货日期**（可以人工指定），不是服务器当天：补录 11-13 的单，
    单号里就该是 20261113，与抬头印的日期一致，也和财务手上的纸对得上。
    """
    prefix = f"{DOC_PREFIX}{day.strftime('%Y%m%d')}"
    # FOR UPDATE 锁住这天已存在的行；InnoDB 的间隙锁同时拦住并发插入同前缀的新行
    existing = db.execute(
        text("SELECT doc_no FROM shipments WHERE doc_no LIKE :p FOR UPDATE"),
        {"p": f"{prefix}%"},
    ).scalars().all()

    seq = 0
    for doc_no in existing:
        tail = doc_no[len(prefix) :]
        if tail.isdigit():
            seq = max(seq, int(tail))
    return f"{prefix}{seq + 1:02d}"


def _unit_price_map(db: Session, shipments: list[Shipment]) -> dict[str, float]:
    """order_id → 合同含税单价。出货明细不存价，金额一律按合同单价 × 出货用量算。"""
    order_ids = {i.order_id for s in shipments for i in s.items if i.order_id}
    if not order_ids:
        return {}
    rows = (
        db.query(ContractItem.order_id, ContractItem.unit_price)
        .filter(ContractItem.order_id.in_(list(order_ids)))
        .all()
    )
    return {oid: (price or 0.0) for oid, price in rows if oid}


def _to_out(shipment: Shipment, prices: dict[str, float]) -> ShipmentOut:
    items: list[ShipmentItemOut] = []
    total_amount = 0.0
    for i in sorted(shipment.items, key=lambda x: x.seq):
        unit_price = prices.get(i.order_id or "", 0.0)
        amount = round(unit_price * i.quantity, 2)
        total_amount += amount
        items.append(
            ShipmentItemOut(
                id=i.id,
                seq=i.seq,
                order_id=i.order_id,
                material_no=i.material_no,
                product=i.product,
                model=i.model,
                quantity=i.quantity,
                unit=i.unit,
                remark=i.remark,
                unit_price=unit_price,
                amount=amount,
            )
        )
    return ShipmentOut(
        id=shipment.id,
        doc_no=shipment.doc_no,
        contract_id=shipment.contract_id,
        project_no=shipment.project_no,
        party_a=shipment.party_a,
        address=shipment.address,
        purchase_no=shipment.purchase_no,
        ship_from=shipment.ship_from,
        created_at=shipment.created_at,
        ship_date=shipment.ship_date or shipment.created_at.date(),
        item_count=len(shipment.items),
        total_quantity=sum(i.quantity for i in shipment.items),
        total_amount=round(total_amount, 2),
        qc_override=shipment.qc_override,
        qc_override_note=shipment.qc_override_note,
        items=items,
    )


@router.get("/contract-progress", response_model=list[ContractProgressOut])
def list_contract_progress(db: Session = Depends(get_db)):
    contracts = (
        db.query(Contract).order_by(Contract.created_at.desc(), Contract.id.desc()).all()
    )
    fulfilled = fulfilled_order_ids(db)
    result = []
    for contract in contracts:
        rows = _load_contract_rows(db, contract)
        shipped = _shipped_doc_map(db, contract.id)
        total_qty = sum(item.quantity for item, _, _ in rows)
        counted = sum(q.get("pass", 0) for _, _, q in rows)
        order_ids = [item.order_id for item, _, _ in rows if item.order_id]
        shipped_qty = shipped_qty_map(db, order_ids)
        shipped_qty_total = sum(shipped_qty.get(oid, 0) for oid in order_ids)
        result.append(
            ContractProgressOut(
                contract_id=contract.id,
                purchase_no=contract.purchase_no,
                party_a=contract.party_a,
                address=contract.address,
                signed_at=contract.signed_at,
                project_count=len({item.project_no for item, _, _ in rows if item.project_no}),
                item_count=len(rows),
                total_quantity=total_qty,
                order_count=sum(1 for _, order, _ in rows if order),
                qc2_pass_quantity=counted,
                # 完成度 = 已出货数量 ÷ 合同数量（出货进度）。以前按质检2 合格数算，
                # 历史补偿出货之后会出现「货都发完了、完成度还是 0%」的鬼话。
                completion_rate=round(shipped_qty_total / total_qty * 100, 1) if total_qty else 0.0,
                qc2_passed_orders=sum(
                    1 for _, _, q in rows if q.get("verdict") == QC2_VERDICT_PASS
                ),
                shipped_orders=sum(1 for item, _, _ in rows if item.order_id in shipped),
                fulfilled_orders=sum(1 for item, _, _ in rows if item.order_id in fulfilled),
                shipment_count=(
                    db.query(func.count(Shipment.id))
                    .filter(Shipment.contract_id == contract.id)
                    .scalar()
                    or 0
                ),
            )
        )
    return result


@router.get("/contracts/{contract_id}/orders", response_model=list[ContractOrderOut])
def list_contract_orders(contract_id: str, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if not contract:
        raise HTTPException(404, f"合同不存在: {contract_id}")

    shipped = _shipped_doc_map(db, contract_id)
    rows = list(_load_contract_rows(db, contract))
    order_ids = [item.order_id for item, _, _ in rows if item.order_id]
    shipped_qty = shipped_qty_map(db, order_ids)
    producing = in_production_order_ids(db, order_ids)
    out = []
    for item, order, q in rows:
        done = shipped_qty.get(item.order_id or "", 0)
        out.append(
            ContractOrderOut(
                order_id=item.order_id or "",
                project_no=item.project_no,
                material_no=item.material_no,
                product=item.product,
                model=item.model,
                unit=item.unit,
                contract_quantity=item.quantity,
                current_step=order.current_step if order else 0,
                order_status=resolve_order_status(order, producing) if order else "",
                qc2_status=q.get("verdict", ""),
                qc2_pass_count=q.get("pass", 0),
                counted_quantity=q.get("pass", 0),
                shipped=done > 0,
                shipped_quantity=done,
                # 完结口径：累计出货数量够数。与 create_shipment 里写 fulfilled_at
                # 用的是同一个比较，两边必须一致，否则界面和实际状态会打架。
                fulfilled=bool(order and order.fulfilled_at),
                remaining_quantity=max(0, item.quantity - done),
                shipment_doc_no=shipped.get(item.order_id, "") if item.order_id else "",
            )
        )
    return out


NOTE_LIMIT = 480  # shipments.qc_override_note 是 varchar(500)，留 20 字符余量


def _override_note(pairs: list[tuple[str, str]]) -> str:
    """把「哪几张工单因为什么被放行」压成一句能塞进 varchar(500) 的话。

    不能逐张列：一张送货单可能一次补上百张历史单，`WO17972AA 未终检、` 这种写法
    重复 101 次就是 1400 多字符，MySQL 严格模式直接 1406 Data too long，整个出货
    事务回滚 —— 前端只看到 500，单子一张都没生成。这里按终检结论分组计数，每组
    只列前 3 个单号；具体哪几张被放行，shipment_items 里逐行都有，随时能反查。
    """
    if not pairs:
        return ""
    by_verdict: dict[str, list[str]] = {}
    for order_id, verdict in pairs:
        by_verdict.setdefault(verdict or "未终检", []).append(order_id)

    parts = []
    for verdict, order_ids in by_verdict.items():
        head = "、".join(order_ids[:3])
        tail = " 等" if len(order_ids) > 3 else ""
        parts.append(f"{verdict} {len(order_ids)} 张（{head}{tail}）")
    note = "；".join(parts)
    return note if len(note) <= NOTE_LIMIT else note[: NOTE_LIMIT - 1] + "…"


@router.post("", response_model=list[ShipmentOut])
def create_shipment(req: ShipmentCreate, db: Session = Depends(get_db)):
    contract = db.query(Contract).filter(Contract.id == req.contract_id).first()
    if not contract:
        raise HTTPException(404, f"合同不存在: {req.contract_id}")

    items_by_order = {
        ci.order_id: ci
        for ci in db.query(ContractItem).filter(ContractItem.contract_id == contract.id)
        if ci.order_id
    }

    groups: dict[str, list] = {}
    seen: set[str] = set()
    for entry in req.items:
        if entry.order_id in seen:
            raise HTTPException(400, f"工单 {entry.order_id} 重复提交")
        seen.add(entry.order_id)
        contract_item = items_by_order.get(entry.order_id)
        if not contract_item:
            raise HTTPException(400, f"工单 {entry.order_id} 不属于合同 {contract.id}")
        project_key = contract_item.project_no or "未指定项目号"
        groups.setdefault(project_key, []).append((entry, contract_item))

    # 分批出货：出过一部分的单还能接着出，但出满了就不能再出 —— 否则累计数量会
    # 超过合同数量，完结状态和实物都对不上。这里按「合同数量」判，与完结口径一致。
    shipped_before = shipped_qty_map(db, [e.order_id for e in req.items])
    already_full = [
        f"{e.order_id}（已出 {shipped_before.get(e.order_id, 0)}/{items_by_order[e.order_id].quantity}）"
        for e in req.items
        if shipped_before.get(e.order_id, 0) >= items_by_order[e.order_id].quantity
    ]
    if already_full:
        raise HTTPException(400, f"以下工单已出满，不能再出货: {'、'.join(already_full)}")

    # 只有终检判定「合格」的工单能出货。前端勾选框也禁掉了未合格的，但那能绕过，
    # 规则得在这里兜住。整批一次性报全，免得用户一条条试。
    #
    # req.qc_override=True 是「历史补偿模式」：历史单大量停在程序/质检环节，终检根本
    # 没做过，要补开出货单只能显式绕过。放行不等于无条件：已出满的仍然拦住（见上），
    # 而且每一张被放行的工单都会把「未终检 / 返工 / 报废 / 维修」写进送货单备注字段，
    # 事后能查。默认 False，日常出货仍按合格才放行的老规则。
    order_ids = [e.order_id for e in req.items]
    qc2 = _qc2_summary(db, order_ids)
    not_passed = [
        (oid, qc2.get(oid, {}).get("verdict", ""))
        for oid in order_ids
        if qc2.get(oid, {}).get("verdict", "") != QC2_VERDICT_PASS
    ]
    if not_passed and not req.qc_override:
        detail = "、".join(
            f"{oid}（{QC2_VERDICT_LABELS.get(v, v)}）" for oid, v in not_passed
        )
        raise HTTPException(400, f"以下工单终检未合格，不能生成出货单: {detail}")
    override_reason = {
        oid: QC2_VERDICT_LABELS.get(v, v) or "未终检" for oid, v in not_passed
    }

    address = req.address.strip()
    if not address:
        raise HTTPException(400, "收货地址不能为空")

    # 出货日期决定单号里那串 yyyymmdd：补录历史单时前端直接传过来，不传就是今天。
    # 同一批拆成多张单时共用这个日期，序号靠 _next_doc_no 依次往后排。
    ship_day = req.ship_date or date.today()

    created = []
    for project_no, entries in groups.items():
        # 放行说明按单张送货单各自记：这张单里错了哪几张，就写哪几张。
        # 用压缩后的描述而不是逐张罗列 —— 逐张列会超出列宽把整批出货炸掉，
        # 详见 _override_note 的注释。
        override_note = _override_note(
            [
                (entry.order_id, override_reason[entry.order_id])
                for entry, _ in entries
                if entry.order_id in override_reason
            ]
        )
        shipment = Shipment(
            doc_no=_next_doc_no(db, ship_day),
            contract_id=contract.id,
            project_no=project_no,
            party_a=contract.party_a,
            address=address,
            purchase_no=contract.purchase_no,
            ship_from=settings.ship_from_company,
            ship_date=ship_day,
            qc_override=bool(override_note),
            qc_override_note=override_note,
        )
        db.add(shipment)
        db.flush()
        for seq, (entry, contract_item) in enumerate(entries, start=1):
            db.add(
                ShipmentItem(
                    shipment_id=shipment.id,
                    order_id=entry.order_id,
                    seq=seq,
                    material_no=contract_item.material_no,
                    product=contract_item.product,
                    model=contract_item.model,
                    quantity=entry.quantity,
                    unit=entry.unit.strip() or contract_item.unit,
                    remark=entry.remark,
                )
            )
        created.append(shipment)

    # 出货后回写工单：current_step 只进不退地推到出货阶段；累计数量够数就写完结时间，
    # 并把流水线里的「出货」步骤标成已完成（补偿单跳过的 3~6 步保持原状，绝不补假记录）。
    order_ids = [e.order_id for e in req.items]
    orders_by_id = {o.id: o for o in db.query(Order).filter(Order.id.in_(order_ids)).all()}
    shipping_steps = {
        s.order_id: s
        for s in db.query(OrderStep)
        .filter(OrderStep.order_id.in_(order_ids), OrderStep.step_key == "shipping")
        .all()
    }
    now = datetime.now()
    for entry in req.items:
        order = orders_by_id.get(entry.order_id)
        if not order:
            continue
        target = items_by_order[entry.order_id].quantity
        total = shipped_before.get(entry.order_id, 0) + entry.quantity
        if order.current_step < SHIPPING_STEP:
            order.current_step = SHIPPING_STEP
        step = shipping_steps.get(order.id)
        if total >= target:
            if order.fulfilled_at is None:
                order.fulfilled_at = now
            if step:
                step.status = "completed"
                step.time = date.today()
        elif step and step.status != "completed":
            step.status = "processing"

    if created:
        audit.record(
            db, action="create", module="shipments",
            target_id=str(created[0].id),
            target_label=f"送货单 {created[0].doc_no}"
                        + (f" 等 {len(created)} 张" if len(created) > 1 else ""),
            summary=f"新建出货：合同 {req.contract_id}，"
                    f"{len(created)} 张送货单，共 {sum(len(s.items) for s in created)} 个工单",
        )
    db.commit()
    for shipment in created:
        db.refresh(shipment)
    prices = _unit_price_map(db, created)
    return [_to_out(s, prices) for s in created]


@router.get("", response_model=list[ShipmentOut])
def list_shipments(contract_id: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Shipment)
    if contract_id:
        query = query.filter(Shipment.contract_id == contract_id)
    shipments = query.order_by(Shipment.created_at.desc(), Shipment.id.desc()).all()
    prices = _unit_price_map(db, shipments)
    return [_to_out(s, prices) for s in shipments]


@router.get("/{shipment_id}", response_model=ShipmentOut)
def get_shipment(shipment_id: int, db: Session = Depends(get_db)):
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        raise HTTPException(404, f"出货记录单不存在: #{shipment_id}")
    return _to_out(shipment, _unit_price_map(db, [shipment]))


@router.patch("/{shipment_id}/ship-date", response_model=ShipmentOut)
def update_ship_date(shipment_id: int, payload: ShipmentDateUpdate, db: Session = Depends(get_db)):
    """改送货单的出货日期，并按新日期重编单号。

    单号规则是「ZSD + 出货日期 + 当天第几张」，所以日期一动，单号必须跟着动 ——
    否则抬头印 11/19、单号却写着 1115，财务对账时一定被问住。

    重编的位置是「目标日期当天末尾」：取该日最大序号 +1，不重排同日的其他单。
    重排会把跟这次操作无关的、可能已经打印给客户的单号一起改掉，代价太大；
    留个空号对账没有任何影响。

    日期没变时不动单号：操作员手滑连点两次保存，序号不该往后跳一位。
    """
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        raise HTTPException(404, f"出货记录单不存在: #{shipment_id}")

    old_date, old_doc_no = shipment.ship_date, shipment.doc_no
    changed = shipment.ship_date != payload.ship_date
    shipment.ship_date = payload.ship_date
    if changed:
        shipment.doc_no = _next_doc_no(db, payload.ship_date)
        # 改出货日期会连带重编单号，是对账相关的高危操作，必须留痕
        audit.record(
            db, action="update", module="shipments",
            target_id=str(shipment.id),
            target_label=f"送货单 {old_doc_no}",
            summary=f"改出货日期：{old_date} → {payload.ship_date}，单号 {old_doc_no} → {shipment.doc_no}",
            changes=[
                {"field": "ship_date", "label": "出货日期", "before": old_date, "after": payload.ship_date},
                {"field": "doc_no", "label": "单号", "before": old_doc_no, "after": shipment.doc_no},
            ],
        )
    db.commit()
    db.refresh(shipment)
    return _to_out(shipment, _unit_price_map(db, [shipment]))


COPY_CN_NUM = {2: "二", 3: "三", 4: "四", 5: "五"}

_PRINT_CSS = """
  /* A4 纵向，左右边距 6mm（原 12mm）：型号最长 29 字符，纵向只有把左右压到 6mm
     才排得下一行。上下仍留 12mm，别把表头顶到纸边上。
     6mm 是给打印机不可打印区留的余量（普通激光机 4~5mm），再小会切到边框。 */
  @page { size: A4; margin: 12mm 6mm; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; font-size: 13px; }
  /* 纸张留白只由 @page（打印）和下面的 @media screen 负责，这里再加 padding 会让
     纸上实际边距变成 6mm+3.2mm，左右和上下就不齐了 */
  .sheet { page-break-after: always; break-after: page; }
  /* 最后一页不再换页，否则打印机会多吐一张白纸出来 */
  .sheet:last-child { page-break-after: auto; break-after: auto; }
  .title { font-size: 17px; font-weight: 700; padding: 6px 0 10px; }
  .head-line { font-size: 13px; padding: 3px 0; }
  .notice { font-size: 12px; color: #333; padding: 8px 0 6px; }
  /* fixed 布局：百分比才是铁律。auto 布局下长型号会把列撑宽、整张表顶出版心被裁掉 */
  table { width: 100%; border-collapse: collapse; margin-top: 4px; table-layout: fixed; }
  /* 续页自动重复表头：46 行的大单第二张纸也有列名 */
  thead { display: table-header-group; }
  th, td { border: 1px solid #000; padding: 5px 6px; font-size: 12px; text-align: center; }
  th { background: #f2f2f2; font-weight: 700; white-space: nowrap; }
  /* 短字段永远一行。品名/规格/备注故意不加 nowrap：真超长时让那**一格**自己折行
     （型号按连字符断，中文按字断），而不是把字挤到隔壁格里盖住用量。
     列序见 sheet_html：1 序号 2 物料号 3 品名 4 规格 5 用量 6 单位 7 备注 */
  td:nth-child(1), td:nth-child(2), td:nth-child(5), td:nth-child(6) { white-space: nowrap; }
  td.num { text-align: right; }
  .sign { display: flex; justify-content: space-between; margin-top: 28px; font-size: 13px; }
  .sign div { width: 45%; }
  .sign .line { border-bottom: 1px solid #000; height: 26px; }
  /* 提示条只给屏幕看：纸上不该多出一行给客户看的说明 */
  .print-hint { background: #fff8e1; border: 1px solid #f0c36d; color: #8a5b00;
                padding: 8px 10px; margin: 12px 12px 0; font-size: 12px; border-radius: 4px; }
  /* 屏幕上的白纸要和 A4 完全一致：宽 210mm、内边距 12mm/6mm，
     这样屏幕看到哪里换行，纸上就是哪里换行 */
  @media screen {
    body { background: #eef1f5; padding-bottom: 16px; }
    .sheet { background: #fff; width: 210mm; padding: 12mm 6mm; margin: 10px auto 0;
             box-shadow: 0 1px 8px rgba(0, 0, 0, .16); }
  }
  @media print { .print-hint { display: none; } }
"""


@router.get("/{shipment_id}/print", response_class=HTMLResponse)
def print_shipment(shipment_id: int, copies: int = 3, db: Session = Depends(get_db)):
    """送货单打印页：把同一份内容重复排成 N 页，一页一张。

    为什么在页面里排 N 页，而不是靠打印对话框的「份数」：
      - window.print() 不带参数，对话框里的「份数」是浏览器自己的控件，页面既读不到
        也设不了——「默认打 3 份」在 Web 上根本没有对应 API；
      - 靠操作员每次手填 3，就会漏（原本正是这样漏的）；
      - 所以把「份数」变成文档的一部分：3 份 = 3 页，对话框显示「3 张纸」，无需记忆。
    代价是对话框份数必须保持 1，否则叠乘（3×3=9 张）。页面顶部因此有一条只在屏幕上
    出现的提示条，@media print 隐藏，纸上不留痕；再借对话框的页数自证。

    页面上不标「客户签收联 / 财务对账联 / 存根联」这类联次：几张纸内容完全一样，
    标了反而要额外解释谁拿哪张。要区分拿谁的那张，靠人工分开即可。

    份数超出 1–5 时静默夹紧，不报 422：现场不该因为多敲一个数就看不到单据。
    """
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        raise HTTPException(404, f"出货记录单不存在: #{shipment_id}")

    n = max(1, min(5, copies))

    items = sorted(shipment.items, key=lambda i: i.seq)
    # 抬头日期取这张单的出货日期，不是打印当天 —— 否则昨天开的单今天补打，
    # 纸上就变成今天，跟单号里写死的开单日期对不上。
    ship_day = shipment.ship_date or shipment.created_at.date()
    printed_on = f"{ship_day.month}/{ship_day.day}"
    rows_html = "".join(
        f"<tr><td>{i.seq}</td><td>{i.material_no}</td><td>{i.product}</td>"
        f"<td>{i.model}</td><td class='num'>{i.quantity}</td><td>{i.unit}</td>"
        f"<td>{i.remark}</td></tr>"
        for i in items
    )
    # 只打 1 张时不写「一式一份」，那句话在只有一张纸时是废话
    copies_note = f"&nbsp;&nbsp;一式{COPY_CN_NUM[n]}份" if n > 1 else ""

    # 每页内容完全相同，不标「客户签收联」之类的联次：现场一份份撕下来就是三张一样的单，
    # 标了反而要多解释谁该拿哪张。
    sheet_html = f"""  <div class="sheet">
    <div class="title">{shipment.ship_from}({shipment.doc_no}){printed_on}</div>
    <div class="head-line">收件公司：{shipment.party_a}</div>
    <div class="head-line">地址：{shipment.address}</div>
    <div class="head-line">采购单号：{shipment.purchase_no}</div>
    <div class="head-line">项目号：{shipment.project_no}</div>
    <div class="notice">以下明细由收件方签字确认后，默认已收货{copies_note}</div>
    <table>
      <thead>
        <!-- 列宽按「12px 微软雅黑下的实际文字宽度 + 单元格 padding」反推，不是拍的百分比。
             版心 = A4 210mm - 左右 6mm = 198mm ≈ 748px：
               序号 6%=45  物料号 10%=75（8 位数字 55px）  品名 17%=127（9 汉字 108px）
               规格/型号 44%=329（29 字符 243px，余量 74px ≈ 能再长 9 个字符）
               用量 6%=45  单位 6%=45（PCS 27px）  备注 11%=82（5 汉字）
             改列宽前先量这条单里最长的型号：`SELECT MAX(CHAR_LENGTH(model)) FROM orders` -->
        <tr><th style="width:6%">序号</th><th style="width:10%">物料号</th><th style="width:17%">品名</th>
        <th style="width:44%">规格/型号</th><th style="width:6%">用量</th><th style="width:6%">单位</th>
        <th style="width:11%">备注</th></tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    <div class="sign">
      <div>送货人：<div class="line"></div></div>
      <div>收件方签字：<div class="line"></div></div>
    </div>
  </div>"""
    sheets = sheet_html * n

    hint = ""
    if n > 1:
        hint = (
            f'<div class="print-hint">本页已含 <strong>{n}</strong> 张内容相同的送货单。'
            f"打印对话框里的「份数」请保持 <strong>1</strong>，"
            f"否则会叠乘成 {n} 倍的纸。</div>"
        )

    # 打印预览标题与 PDF 默认文件名都取 <title>，带上页数便于存档时一眼认出
    title_suffix = f"（{n}页）" if n > 1 else ""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>送货单 — {shipment.doc_no}{title_suffix}</title>
<style>{_PRINT_CSS}</style>
</head>
<body>
{hint}
{sheets}
</body>
</html>"""
    return HTMLResponse(content=html)


def _short_customer(name: str) -> str:
    """客户简称，给对账单文件名用：苏州泰松智能科技有限公司 → 苏州泰松。

    只取前 4 个字符，不去猜「科技/智能/精密」这些词该怎么剥 —— 猜错了文件名
    比长一点更难受。用户可以自己改。
    """
    text = (name or "").replace(" ", "").strip()
    return text[:4] if text else "客户"


def _statement_rows(db: Session, shipments: list[Shipment]) -> list[dict]:
    """把若干张送货单的明细摊平成对账单的行。

    不合并同一工单的多次出货：对账要能一行行对回送货单，合并了就查不出差异。
    单价和品牌只有合同明细（contract_items）有，按「工单 + 项目号」取，取不到
    再退回只按工单取 —— 同一工单在多份合同里重号时，前者才对。
    """
    ids = [s.id for s in shipments]
    if not ids:
        return []
    pairs = (
        db.query(ShipmentItem, Order)
        .outerjoin(Order, Order.id == ShipmentItem.order_id)
        .filter(ShipmentItem.shipment_id.in_(ids))
        .all()
    )
    order_ids = sorted({item.order_id for item, _ in pairs if item.order_id})
    by_project: dict[tuple[str, str], tuple[str, float]] = {}
    by_order: dict[str, tuple[str, float]] = {}
    if order_ids:
        for ci in db.query(ContractItem).filter(ContractItem.order_id.in_(order_ids)).all():
            value = (ci.brand or "", ci.unit_price or 0.0)
            by_project.setdefault((ci.order_id, ci.project_no or ""), value)
            by_order.setdefault(ci.order_id, value)

    grouped: dict[int, list[ShipmentItem]] = {}
    for item, _order in pairs:
        grouped.setdefault(item.shipment_id, []).append(item)

    rows: list[dict] = []
    for sh in shipments:
        for item in sorted(grouped.get(sh.id, []), key=lambda i: i.seq):
            brand, price = by_project.get(
                (item.order_id or "", sh.project_no or ""),
                by_order.get(item.order_id or "", ("", 0.0)),
            )
            qty = item.quantity or 0
            rows.append(
                {
                    "material_no": item.material_no,
                    "product": item.product,
                    "model": item.model,
                    "brand": brand,
                    "quantity": qty,
                    "unit_price": price,
                    "amount": round(qty * price, 2),
                    # 备注（订单号）印客户的采购单号：同一客户的采购单号不会重复，
                    # 所以不需要模板里那种 -1 后缀来区分
                    "purchase_no": sh.purchase_no,
                    "project_no": sh.project_no,
                }
            )
    return rows


@public_router.get("/{shipment_id}/statement", response_class=HTMLResponse)
def print_statement(
    shipment_id: int,
    scope: str = "month",
    title: str | None = None,
    token: str = Query(default=""),
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    """客户对账单。scope=month 把该客户当月所有送货单拼成一张，scope=single 只本单。

    为什么按「客户 + 月份」聚合：客户的账是一月一对，同一客户同月有三张送货单时，
    从哪一行点进来都该得到同一张表；做单张的话财务还得自己拼。

    月份口径取**出货日期**（ship_date）而不是开单时间 —— 送货单抬头印的是出货日期，
    对账要跟客户手上的纸对得上。ship_date 为空的老单退回 created_at，与
    backfill_ship_date 的补值一致。

    鉴权：这个页面是「新标签页直达 URL」打开的，浏览器带不了 Authorization 头，
    所以接受 ?token= 参数（或 header 里的 Bearer）。对账单含客户名与金额，
    不能像以前那样放开。
    """
    _require_print_token(token or authorization)
    anchor = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not anchor:
        raise HTTPException(404, f"出货记录单不存在: #{shipment_id}")

    day = anchor.ship_date or anchor.created_at.date()
    first_day = day.replace(day=1)
    next_month = (first_day + timedelta(days=32)).replace(day=1)
    ship_day = func.coalesce(Shipment.ship_date, func.date(Shipment.created_at))

    month_shipments = (
        db.query(Shipment)
        .filter(
            Shipment.party_a == anchor.party_a,
            ship_day >= first_day,
            ship_day < next_month,
        )
        .order_by(ship_day, Shipment.doc_no)
        .all()
    )
    if scope == "single":
        picked = [anchor]
    else:
        picked = month_shipments
        scope = "month"

    rows = _statement_rows(db, picked)
    total = round(sum(r["amount"] for r in rows), 2)
    period = f"{day.year}年{day.month}月"
    short = _short_customer(anchor.party_a)
    default_title = (
        f"{short}-{anchor.doc_no}-对账单"
        if scope == "single"
        else f"{short}-{period}对账单"
    )
    empty_hint = (
        f"本单没有出货明细（{anchor.doc_no}）"
        if scope == "single"
        else f"{period}没有 {anchor.party_a} 的出货记录"
    )
    return HTMLResponse(
        content=render_statement(
            customer=anchor.party_a,
            supplier=anchor.ship_from,
            period=period,
            rows=rows,
            total=total,
            title=(title or "").strip() or default_title,
            scope=scope,
            month_label=f"{anchor.party_a} · {period}（共 {len(month_shipments)} 张送货单）",
            single_label=f"仅本单 {anchor.doc_no}",
            empty_hint=empty_hint,
        )
    )

