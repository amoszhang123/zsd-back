from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
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
    ShipmentItemOut,
    ShipmentOut,
)

router = APIRouter(prefix="/api/shipments", tags=["shipments"])

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
    prefix = f"{DOC_PREFIX}{day.strftime('%Y%m%d')}"
    seq = (
        db.query(func.count(Shipment.id)).filter(Shipment.doc_no.like(f"{prefix}%")).scalar() or 0
    ) + 1
    candidate = f"{prefix}{seq:02d}"
    while db.query(Shipment.id).filter(Shipment.doc_no == candidate).first():
        seq += 1
        candidate = f"{prefix}{seq:02d}"
    return candidate


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

    created = []
    for project_no, entries in groups.items():
        # 放行说明按单张送货单各自记：这张单里错了哪几张，就写哪几张
        override_note = "、".join(
            f"{entry.order_id} {override_reason[entry.order_id]}"
            for entry, _ in entries
            if entry.order_id in override_reason
        )
        shipment = Shipment(
            doc_no=_next_doc_no(db, date.today()),
            contract_id=contract.id,
            project_no=project_no,
            party_a=contract.party_a,
            address=address,
            purchase_no=contract.purchase_no,
            ship_from=settings.ship_from_company,
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


@router.get("/{shipment_id}/print", response_class=HTMLResponse)
def print_shipment(shipment_id: int, db: Session = Depends(get_db)):
    """送货单打印页，只排 1 份内容。

    「一式三份」是打印机的份数参数，由操作员在打印对话框里自己设，不在页面里把
    内容复制 3 遍来凑 —— 那样对话框里再设一次份数就会叠乘（3×3=9 张），存档成
    PDF 也会莫名变成 3 页。
    """
    shipment = db.query(Shipment).filter(Shipment.id == shipment_id).first()
    if not shipment:
        raise HTTPException(404, f"出货记录单不存在: #{shipment_id}")

    items = sorted(shipment.items, key=lambda i: i.seq)
    today = date.today()
    printed_on = f"{today.month}/{today.day}"
    rows_html = "".join(
        f"<tr><td>{i.seq}</td><td>{i.material_no}</td><td>{i.product}</td>"
        f"<td>{i.model}</td><td class='num'>{i.quantity}</td><td>{i.unit}</td>"
        f"<td>{i.remark}</td></tr>"
        for i in items
    )

    sheet_html = f"""  <div class="title">{shipment.ship_from}({shipment.doc_no}){printed_on}</div>
  <div class="head-line">收件公司：{shipment.party_a}</div>
  <div class="head-line">地址：{shipment.address}</div>
  <div class="head-line">采购单号：{shipment.purchase_no}</div>
  <div class="head-line">项目号：{shipment.project_no}</div>
  <div class="notice">以下明细由收件方签字确认后，默认已收货&nbsp;&nbsp;一式三份</div>
  <table>
    <thead>
      <tr><th style="width:7%">序号</th><th style="width:13%">物料号</th><th style="width:18%">品名</th>
      <th style="width:26%">规格/型号</th><th style="width:10%">用量</th><th style="width:9%">单位</th>
      <th style="width:17%">备注</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <div class="sign">
    <div>送货人：<div class="line"></div></div>
    <div>收件方签字：<div class="line"></div></div>
  </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>送货单 — {shipment.doc_no}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; font-size: 13px; }}
  .sheet {{ padding: 12px; }}
  .title {{ font-size: 17px; font-weight: 700; padding: 6px 0 10px; }}
  .head-line {{ font-size: 13px; padding: 3px 0; }}
  .notice {{ font-size: 12px; color: #333; padding: 8px 0 6px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 4px; }}
  th, td {{ border: 1px solid #000; padding: 5px 6px; font-size: 12px; text-align: center; }}
  th {{ background: #f2f2f2; font-weight: 700; }}
  td.num {{ text-align: right; }}
  .sign {{ display: flex; justify-content: space-between; margin-top: 28px; font-size: 13px; }}
  .sign div {{ width: 45%; }}
  .sign .line {{ border-bottom: 1px solid #000; height: 26px; }}
</style>
</head>
<body>
  <div class="sheet">
{sheet_html}
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)
