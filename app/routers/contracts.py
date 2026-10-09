import re
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.contract import (
    ALLOWED_STATUS_TRANSITIONS,
    CONTRACT_STATUS_CANCELLED,
    Contract,
    ContractItem,
    contract_in_production,
    in_production_map,
)
from app.models.order import Order
from app.models.production import WorkOrderItem
from app.routers.orders import _build_steps
from app.schemas.contract import (
    ContractDetail,
    ContractImport,
    ContractItemOut,
    ContractItemQuantityUpdate,
    ContractStatusUpdate,
    ContractSummary,
)

router = APIRouter(prefix="/api/contracts", tags=["contracts"])


def _new_order_id(base: int, idx: int, taken: set[str]) -> str:
    """沿用现有 WO{n:05d}{字母}{字母} 格式；base 是毫秒时间戳，%100000 每 100 秒循环一次，
    因此必须查重重试，否则批量导入会撞主键。"""
    for attempt in range(2000):
        num = (base + idx + attempt) % 100000
        candidate = f"WO{num:05d}{chr(65 + idx % 26)}{chr(65 + (idx // 26) % 26)}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise HTTPException(500, "工单号已用尽，请稍后重试")


def _to_summary(contract: Contract, in_production: bool = False) -> ContractSummary:
    return ContractSummary(
        id=contract.id,
        purchase_no=contract.purchase_no,
        status=contract.status,
        in_production=in_production,
        party_a=contract.party_a,
        address=contract.address,
        bank_name=contract.bank_name,
        bank_account=contract.bank_account,
        tax_no=contract.tax_no,
        phone=contract.phone,
        signed_at=contract.signed_at,
        total_amount=contract.total_amount,
        source_file=contract.source_file,
        item_count=len(contract.items),
        total_quantity=sum(item.quantity for item in contract.items),
        created_at=contract.created_at,
    )


def _parse_delivery_date(value: str) -> date | None:
    """把合同明细的交期解析成日期；解析不出来就返回 None，绝不猜。

    「1」「2」这类裸数字必须返回 None：它们既不是日期，也不能当 Excel 序列号
    （序列号 1 会变成 1900-01-01）。宁可让工单没有交期，也不要把脏数据
    伪造成一个看起来合理的日期 —— 那会直接污染生产管理的超期预警。
    """
    s = str(value or "").strip()
    if not s:
        return None

    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", s)
    if m:
        year = int(m.group(3))
        year = year + 2000 if year < 100 else year
        try:
            return date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None

    # 纯数字只在明显是 Excel 日期序列号时才接受（20000 ≈ 1954 年，80000 ≈ 2118 年）
    if s.isdigit() and 20000 <= int(s) <= 80000:
        return date(1899, 12, 30) + timedelta(days=int(s))

    return None


def _locked_item_ids(db: Session, contract: Contract) -> set[int]:
    """已进过开工单的明细 id。这些明细的数量不允许再改，否则会与实际生产记录对不上。"""
    order_ids = [i.order_id for i in contract.items if i.order_id]
    if not order_ids:
        return set()
    worked = {
        row[0]
        for row in db.query(WorkOrderItem.order_id)
        .filter(WorkOrderItem.order_id.in_(order_ids))
        .distinct()
        .all()
    }
    return {i.id for i in contract.items if i.order_id in worked}


def _order_deadline_map(db: Session, contract: Contract) -> dict[str, date | None]:
    """order_id → 工单交期，一次查完，避免逐条明细懒加载。"""
    order_ids = [i.order_id for i in contract.items if i.order_id]
    if not order_ids:
        return {}
    rows = db.query(Order.id, Order.deadline).filter(Order.id.in_(order_ids)).all()
    return {oid: deadline for oid, deadline in rows}


def _to_detail(
    db: Session, contract: Contract, locked_ids: set[int] | None = None
) -> ContractDetail:
    locked_ids = locked_ids or set()
    deadlines = _order_deadline_map(db, contract)

    items = []
    synced = 0
    unparsed = 0
    for i in contract.items:
        deadline = deadlines.get(i.order_id) if i.order_id else None
        if deadline is not None:
            synced += 1
        if i.delivery.strip() and _parse_delivery_date(i.delivery) is None:
            unparsed += 1
        items.append(
            ContractItemOut.model_validate(i).model_copy(
                update={"locked": i.id in locked_ids, "order_deadline": deadline}
            )
        )

    return ContractDetail(
        **_to_summary(contract, contract_in_production(db, contract)).model_dump(),
        deadline_synced=synced,
        delivery_unparsed=unparsed,
        items=items,
    )


@router.post("/import", response_model=ContractDetail)
def import_contract(payload: ContractImport, db: Session = Depends(get_db)):
    purchase_no = payload.purchase_no.strip()
    if not purchase_no:
        raise HTTPException(400, "采购单号不能为空，无法生成合同id")

    contract_id = f"HT{purchase_no}"
    if db.query(Contract).filter(Contract.id == contract_id).first():
        raise HTTPException(409, f"合同 {contract_id}（采购单号 {purchase_no}）已存在，不能重复导入")

    contract = Contract(
        id=contract_id,
        purchase_no=purchase_no,
        party_a=payload.party_a.strip(),
        address=payload.address.strip(),
        bank_name=payload.bank_name.strip(),
        bank_account=payload.bank_account.strip(),
        tax_no=payload.tax_no.strip(),
        phone=payload.phone.strip(),
        signed_at=payload.signed_at,
        total_amount=payload.total_amount,
        source_file=payload.source_file.strip(),
    )
    db.add(contract)
    db.flush()

    taken = {row[0] for row in db.query(Order.id).all()}
    base = int(time.time() * 1000)
    order_ids: list[str] = []

    for idx, item in enumerate(payload.items):
        order_id = _new_order_id(base, idx, taken)
        order_ids.append(order_id)
        db.add(
            Order(
                id=order_id,
                order_no=purchase_no,
                project_no=item.project_no,
                material_no=item.material_no,
                customer=contract.party_a,
                product=item.product,
                model=item.model,
                quantity=item.quantity,
                current_step=0,
                create_date=date.today(),
                # 交期能解析成日期才写入工单；解析不出就留空，不猜
                deadline=_parse_delivery_date(item.delivery),
            )
        )
        db.add(
            ContractItem(
                contract_id=contract_id,
                seq=item.seq or idx + 1,
                project_no=item.project_no,
                brand=item.brand,
                product=item.product,
                material_no=item.material_no,
                model=item.model,
                unit=item.unit,
                quantity=item.quantity,
                unit_price=item.unit_price,
                amount=item.amount,
                delivery=item.delivery,
                order_id=order_id,
            )
        )

    for order_id in order_ids:
        db.add_all(_build_steps(order_id))

    db.commit()
    db.refresh(contract)
    return _to_detail(db, contract)


@router.get("", response_model=list[ContractSummary])
def list_contracts(db: Session = Depends(get_db)):
    contracts = (
        db.query(Contract)
        .options(joinedload(Contract.items))
        .order_by(Contract.created_at.desc(), Contract.id.desc())
        .all()
    )
    producing = in_production_map(db, [c.id for c in contracts])
    return [_to_summary(c, c.id in producing) for c in contracts]


@router.get("/{contract_id}", response_model=ContractDetail)
def get_contract(contract_id: str, db: Session = Depends(get_db)):
    contract = (
        db.query(Contract)
        .options(joinedload(Contract.items))
        .filter(Contract.id == contract_id)
        .first()
    )
    if not contract:
        raise HTTPException(404, f"合同不存在: {contract_id}")
    return _to_detail(db, contract, _locked_item_ids(db, contract))


@router.patch("/{contract_id}/items/{item_id}", response_model=ContractDetail)
def update_contract_item_quantity(
    contract_id: str,
    item_id: int,
    req: ContractItemQuantityUpdate,
    db: Session = Depends(get_db),
):
    """修改单条合同明细的数量，并同步到对应工单。

    已进过开工单的明细拒绝修改（409）：数量一旦进入生产记录，再改会让完工数量、
    质检批次和进度对不上。
    """
    contract = (
        db.query(Contract)
        .options(joinedload(Contract.items))
        .filter(Contract.id == contract_id)
        .first()
    )
    if not contract:
        raise HTTPException(404, f"合同不存在: {contract_id}")

    item = next((i for i in contract.items if i.id == item_id), None)
    if not item:
        raise HTTPException(404, f"合同 {contract_id} 下没有明细 #{item_id}")

    if item.order_id and (
        db.query(WorkOrderItem.id).filter(WorkOrderItem.order_id == item.order_id).first()
    ):
        raise HTTPException(409, f"工单 {item.order_id} 已进入开工单，数量不可修改")

    item.quantity = req.quantity
    item.amount = round(req.quantity * item.unit_price, 2)
    item.updated_at = datetime.now()

    if item.order_id:
        order = db.get(Order, item.order_id)
        if order:
            order.quantity = req.quantity

    contract.total_amount = round(sum(i.amount for i in contract.items), 2)

    db.commit()
    db.refresh(contract)
    return _to_detail(db, contract, _locked_item_ids(db, contract))


@router.patch("/{contract_id}/status", response_model=ContractDetail)
def update_contract_status(
    contract_id: str, req: ContractStatusUpdate, db: Session = Depends(get_db)
):
    """变更合同状态。

    流转规则：正常 ⇄ 暂停 可来回；取消、结案是终态不可逆。
    已投产（任一工单进过开工单）的合同不能取消，只能暂停后等结款再结案 ——
    东西已经在做了，取消对不上实物。
    """
    contract = (
        db.query(Contract)
        .options(joinedload(Contract.items))
        .filter(Contract.id == contract_id)
        .first()
    )
    if not contract:
        raise HTTPException(404, f"合同不存在: {contract_id}")

    current, target = contract.status, req.status
    if target == current:
        return _to_detail(db, contract, _locked_item_ids(db, contract))

    allowed = ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        if not allowed:
            raise HTTPException(409, f"合同已是「{current}」终态，不能再变更状态")
        raise HTTPException(409, f"合同状态不能从「{current}」变更为「{target}」")

    if target == CONTRACT_STATUS_CANCELLED and contract_in_production(db, contract):
        raise HTTPException(
            409, f"合同 {contract_id} 已投产（有工单进过开工单），不能取消，只能暂停后结案"
        )

    contract.status = target
    db.commit()
    db.refresh(contract)
    return _to_detail(db, contract, _locked_item_ids(db, contract))
