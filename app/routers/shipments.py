from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.contract import Contract, ContractItem
from app.models.order import Order, resolve_order_status
from app.models.production import in_production_order_ids
from app.models.quality import QcInspection
from app.models.shipment import Shipment, ShipmentItem
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
        items=items,
    )


@router.get("/contract-progress", response_model=list[ContractProgressOut])
def list_contract_progress(db: Session = Depends(get_db)):
    contracts = (
        db.query(Contract).order_by(Contract.created_at.desc(), Contract.id.desc()).all()
    )
    result = []
    for contract in contracts:
        rows = _load_contract_rows(db, contract)
        shipped = _shipped_doc_map(db, contract.id)
        total_qty = sum(item.quantity for item, _, _ in rows)
        counted = sum(q.get("pass", 0) for _, _, q in rows)
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
                completion_rate=round(counted / total_qty * 100, 1) if total_qty else 0.0,
                qc2_passed_orders=sum(
                    1 for _, _, q in rows if q.get("verdict") == QC2_VERDICT_PASS
                ),
                shipped_orders=sum(1 for item, _, _ in rows if item.order_id in shipped),
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
    producing = in_production_order_ids(
        db, [item.order_id for item, order, _ in rows if item.order_id]
    )
    out = []
    for item, order, q in rows:
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
                shipped=bool(item.order_id and item.order_id in shipped),
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

    # 只有终检判定「合格」的工单能出货。前端勾选框也禁掉了未合格的，但那能绕过，
    # 规则得在这里兜住。整批一次性报全，免得用户一条条试。
    qc2 = _qc2_summary(db, [e.order_id for e in req.items])
    not_passed = [
        (e.order_id, qc2.get(e.order_id, {}).get("verdict", ""))
        for e in req.items
        if qc2.get(e.order_id, {}).get("verdict", "") != QC2_VERDICT_PASS
    ]
    if not_passed:
        detail = "、".join(
            f"{oid}（{QC2_VERDICT_LABELS.get(v, v)}）" for oid, v in not_passed
        )
        raise HTTPException(400, f"以下工单终检未合格，不能生成出货单: {detail}")

    address = req.address.strip()
    if not address:
        raise HTTPException(400, "收货地址不能为空")

    created = []
    for project_no, entries in groups.items():
        shipment = Shipment(
            doc_no=_next_doc_no(db, date.today()),
            contract_id=contract.id,
            project_no=project_no,
            party_a=contract.party_a,
            address=address,
            purchase_no=contract.purchase_no,
            ship_from=settings.ship_from_company,
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
