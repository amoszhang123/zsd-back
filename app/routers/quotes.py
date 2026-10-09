from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import tuple_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.contract import ContractItem
from app.models.order import STEP_KEYS, Order, OrderStep
from app.models.quote import Quote, QuoteItem
from app.schemas.quote import (
    QuoteDetail,
    QuoteHistoryRow,
    QuoteImport,
    QuoteItemHistory,
    QuoteItemOut,
    QuoteItemUpdate,
    QuoteSummary,
)

router = APIRouter(prefix="/api/quotes", tags=["quotes"])

ACCEPT_STEP = STEP_KEYS.index("order")
# 走到备料及之后即视为已确认接单（待确认是 step 0）
CONFIRMED_STEP = STEP_KEYS.index("material")


def _sorted_items(items) -> list[QuoteItem]:
    return sorted(items, key=lambda i: (i.seq, i.id))


def _new_quote_id(db: Session) -> str:
    """报价记录单 id = 日期 + 毫秒时间戳，撞号时追加序号兜底。"""
    now = datetime.now()
    base = f"BJ{now:%Y%m%d}-{int(now.timestamp() * 1000)}"
    candidate = base
    seq = 1
    while db.query(Quote.id).filter(Quote.id == candidate).first():
        candidate = f"{base}-{seq}"
        seq += 1
    return candidate


def _item_out(item: QuoteItem) -> QuoteItemOut:
    return QuoteItemOut(
        id=item.id,
        seq=item.seq,
        category=item.category,
        material_no=item.material_no,
        product=item.product,
        model=item.model,
        quantity=item.quantity,
        unit=item.unit,
        brand=item.brand,
        price=item.price,
        # 总价 = 报价 × 用量，实时算不落库，避免与手填的报价对不上
        total_price=round(item.price * item.quantity, 2) if item.price is not None else None,
        delivery=item.delivery,
    )


def _summary(quote: Quote) -> QuoteSummary:
    priced = [i for i in quote.items if i.price is not None]
    return QuoteSummary(
        id=quote.id,
        source_file=quote.source_file,
        created_at=quote.created_at,
        item_count=len(quote.items),
        priced_count=len(priced),
        total_amount=round(sum(i.price * i.quantity for i in priced), 2),
    )


def _detail(quote: Quote) -> QuoteDetail:
    return QuoteDetail(
        **_summary(quote).model_dump(),
        items=[_item_out(i) for i in _sorted_items(quote.items)],
    )


@router.post("/import", response_model=QuoteDetail)
def import_quote(payload: QuoteImport, db: Session = Depends(get_db)):
    quote = Quote(id=_new_quote_id(db), source_file=payload.source_file.strip())
    db.add(quote)
    db.flush()

    for idx, item in enumerate(payload.items):
        db.add(
            QuoteItem(
                quote_id=quote.id,
                seq=item.seq or idx + 1,
                category=item.category.strip(),
                material_no=item.material_no.strip(),
                product=item.product.strip(),
                model=item.model.strip(),
                quantity=item.quantity,
                unit=item.unit.strip(),
                brand=item.brand.strip(),
                price=item.price,
                delivery=item.delivery.strip(),
            )
        )

    db.commit()
    db.refresh(quote)
    return _detail(quote)


@router.get("", response_model=list[QuoteSummary])
def list_quotes(db: Session = Depends(get_db)):
    quotes = (
        db.query(Quote)
        .options(joinedload(Quote.items))
        .order_by(Quote.created_at.desc(), Quote.id.desc())
        .all()
    )
    return [_summary(q) for q in quotes]


@router.get("/{quote_id}", response_model=QuoteDetail)
def get_quote(quote_id: str, db: Session = Depends(get_db)):
    quote = (
        db.query(Quote).options(joinedload(Quote.items)).filter(Quote.id == quote_id).first()
    )
    if not quote:
        raise HTTPException(404, f"报价记录单不存在: {quote_id}")
    return _detail(quote)


@router.patch("/{quote_id}/items/{item_id}", response_model=QuoteItemOut)
def update_quote_item(
    quote_id: str, item_id: int, req: QuoteItemUpdate, db: Session = Depends(get_db)
):
    item = (
        db.query(QuoteItem)
        .filter(QuoteItem.id == item_id, QuoteItem.quote_id == quote_id)
        .first()
    )
    if not item:
        raise HTTPException(404, f"报价明细不存在: #{item_id}")

    # 用 model_fields_set 区分「没传」和「显式传 null（清空报价）」
    if "price" in req.model_fields_set:
        item.price = req.price
    if "delivery" in req.model_fields_set:
        item.delivery = (req.delivery or "").strip()

    db.commit()
    db.refresh(item)
    return _item_out(item)


@router.get("/{quote_id}/history", response_model=list[QuoteItemHistory])
def quote_history(quote_id: str, db: Session = Depends(get_db)):
    """物料号 + 品名 + 规格/型号 三项全同、且工单已确认接单的历史报价，按接单日期倒序。

    物料号为空的明细不参与匹配，否则会和历史上所有空物料号的记录乱配。
    """
    quote = (
        db.query(Quote).options(joinedload(Quote.items)).filter(Quote.id == quote_id).first()
    )
    if not quote:
        raise HTTPException(404, f"报价记录单不存在: {quote_id}")

    items = _sorted_items(quote.items)
    triples = {(i.material_no, i.product, i.model) for i in items if i.material_no}
    if not triples:
        return []

    rows = (
        db.query(ContractItem, Order, OrderStep)
        .join(Order, Order.id == ContractItem.order_id)
        .outerjoin(
            OrderStep,
            (OrderStep.order_id == Order.id) & (OrderStep.sort_order == ACCEPT_STEP),
        )
        .filter(
            tuple_(ContractItem.material_no, ContractItem.product, ContractItem.model).in_(
                list(triples)
            ),
            Order.current_step >= CONFIRMED_STEP,
        )
        .all()
    )

    grouped: dict[tuple, list[QuoteHistoryRow]] = {}
    for contract_item, order, step in rows:
        accepted = step.time if step and step.time else order.create_date
        key = (contract_item.material_no, contract_item.product, contract_item.model)
        row = QuoteHistoryRow(
            order_id=order.id, unit_price=contract_item.unit_price, accepted_date=accepted
        )
        grouped.setdefault(key, []).append(row)
    for history in grouped.values():
        history.sort(key=lambda r: r.accepted_date or date.min, reverse=True)

    out = []
    for item in items:
        history = grouped.get((item.material_no, item.product, item.model))
        if item.material_no and history:
            out.append(
                QuoteItemHistory(
                    item_id=item.id,
                    material_no=item.material_no,
                    product=item.product,
                    model=item.model,
                    history=history,
                )
            )
    return out
