"""订单收入接口：按「客户 + 月」把出货额确认成收入。

金额口径与送货单、对账单一致 —— 合同明细的含税单价 × 出货用量。
单价从 contract_items 里取，出货明细本身不存价（见 models/shipment.py 的设计）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit
from app.database import get_db, scoped
from app.models.contract import ContractItem
from app.models.revenue import RevenueRecord
from app.models.shipment import Shipment, ShipmentItem
from app.schemas.revenue import (
    PendingRevenueOut,
    RevenueBoardOut,
    RevenueConfirmIn,
    RevenueOut,
    RevenueUpdate,
)

router = APIRouter(prefix="/api/revenue", tags=["revenue"])

# 金额比较的容差。金额是浮点，sum 之后可能差几分钱；比到 0.005（半分）就够，
# 比到 0 会把「出了 10 万、确认 10 万」判成还差 0.0000001 而永远消不掉待确认。
EPS = 0.005


def _month_expr():
    """出货月份：按送货单的出货日期归月，不是开单时间。

    补录的历史送货单要归到它实际发生的那个月，否则对账单期间与收入月份错位。
    """
    return func.date_format(Shipment.ship_date, "%Y-%m")


def _price_subquery(db: Session):
    """order_id → 合同含税单价 的子查询。

    先按 order_id 聚合再 join，而不是直接 join contract_items：
    万一同一个工单在合同明细里出现两行，直接 join 会把出货数量乘两遍，
    收入凭空翻倍；这里取 max 单价，至少不会翻倍（真实重复数据另有校验，不在本期）。
    scoped 必须写在子查询的 filter 里 —— 只查列/聚合的语句不走 with_loader_criteria。
    """
    return (
        db.query(
            ContractItem.order_id.label("order_id"),
            func.max(ContractItem.unit_price).label("unit_price"),
        )
        .filter(ContractItem.order_id.isnot(None), scoped(db, ContractItem))
        .group_by(ContractItem.order_id)
        .subquery()
    )


def shipped_map(db: Session, period: str | None = None) -> dict[tuple[str, str], float]:
    """(客户, 月份) → 该期出货总额（含税）。

    一张都还没出的月份不会出现在这里，所以也不会有待确认行 —— 不猜预期收入。
    """
    month = _month_expr()
    price = _price_subquery(db)
    q = (
        db.query(
            Shipment.party_a.label("customer"),
            month.label("period"),
            func.sum(
                func.coalesce(price.c.unit_price, 0) * ShipmentItem.quantity
            ).label("amount"),
        )
        .join(ShipmentItem, ShipmentItem.shipment_id == Shipment.id)
        .outerjoin(price, price.c.order_id == ShipmentItem.order_id)
        .filter(Shipment.ship_date.isnot(None))
        .group_by(Shipment.party_a, month)
    )
    if period:
        q = q.filter(month == period)
    return {
        (row.customer or "", row.period): float(row.amount or 0) for row in q.all()
    }


def confirmed_map(db: Session, period: str | None = None) -> dict[tuple[str, str], float]:
    """(客户, 月份) → 已确认的收入合计（同一期可能有多笔，含补录差额）。"""
    q = db.query(
        RevenueRecord.customer,
        RevenueRecord.period,
        func.sum(RevenueRecord.amount),
    ).filter(scoped(db, RevenueRecord))
    if period:
        q = q.filter(RevenueRecord.period == period)
    q = q.group_by(RevenueRecord.customer, RevenueRecord.period)
    return {(c or "", p): float(a or 0) for c, p, a in q.all()}


def _to_out(record: RevenueRecord) -> RevenueOut:
    amount = float(record.amount or 0)
    received = float(record.received_amount or 0)
    return RevenueOut(
        id=record.id,
        customer=record.customer or "",
        period=record.period,
        amount=amount,
        shipped_amount=float(record.shipped_amount or 0),
        invoiced=bool(record.invoiced),
        received_amount=received,
        received_on=record.received_on,
        outstanding=round(amount - received, 2),
        remark=record.remark or "",
        confirmed_at=record.confirmed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def board(db: Session) -> RevenueBoardOut:
    shipped = shipped_map(db)
    confirmed = confirmed_map(db)

    pending: list[PendingRevenueOut] = []
    for (customer, period), total in shipped.items():
        done = confirmed.get((customer, period), 0.0)
        remaining = round(total - done, 2)
        if remaining > EPS:
            pending.append(
                PendingRevenueOut(
                    customer=customer,
                    period=period,
                    shipped_amount=round(total, 2),
                    confirmed_amount=round(done, 2),
                    remaining=remaining,
                )
            )
    # 期间倒序（最近的要先看到），同期内按客户名正序。
    # 用两次排序而不是一个元组 key，是为了让 reverse 只作用于期间。
    pending.sort(key=lambda r: r.customer)
    pending.sort(key=lambda r: r.period, reverse=True)

    rows = db.query(RevenueRecord).order_by(
        RevenueRecord.period.desc(), RevenueRecord.customer.asc(), RevenueRecord.id.asc()
    )
    confirmed_rows = [_to_out(r) for r in rows.all()]
    return RevenueBoardOut(pending=pending, confirmed=confirmed_rows)


def _remaining(db: Session, customer: str, period: str, exclude_amount: float = 0.0) -> tuple[float, float, float]:
    """该客户该期还能确认多少。返回 (出货总额, 已确认额, 可确认余额)。

    exclude_amount 用于「改金额」：把自己原来那笔先扣掉再算余额。
    """
    shipped = shipped_map(db, period).get((customer, period), 0.0)
    done = confirmed_map(db, period).get((customer, period), 0.0) - exclude_amount
    return shipped, done, shipped - done


def confirm_revenue(req: RevenueConfirmIn, db: Session) -> RevenueOut:
    """确认一笔收入。金额不能超过该客户该期的待确认余额。

    这道校验是防重复提交的最后一道闸：双击「确认」不会记成两笔。
    """
    shipped, done, remaining = _remaining(db, req.customer, req.period)
    if remaining <= EPS:
        raise HTTPException(
            400,
            f"{req.customer} {req.period} 没有待确认的收入了"
            f"（出货 {shipped:.2f}，已确认 {done:.2f}）",
        )
    if req.amount > remaining + EPS:
        raise HTTPException(
            400,
            f"确认金额 {req.amount:.2f} 超过待确认余额 {remaining:.2f}"
            f"（出货 {shipped:.2f}，已确认 {done:.2f}）",
        )

    record = RevenueRecord(
        customer=req.customer,
        period=req.period,
        amount=req.amount,
        shipped_amount=round(shipped, 2),
        remark=req.remark,
    )
    db.add(record)
    db.flush()
    audit.record(
        db, action="approve", module="revenue",
        target_id=str(record.id),
        target_label=f"收入 {record.customer} {record.period} ¥{record.amount:g}",
        summary=f"确认收入：{record.customer} {record.period} ¥{record.amount:g}",
    )
    db.commit()
    db.refresh(record)
    return _to_out(record)


def update_revenue_record(
    record: RevenueRecord, req: RevenueUpdate, db: Session
) -> RevenueOut:
    """改已确认的收入（金额、开票、收款、备注）。金额会重新校验上限。"""
    changes = req.model_dump(exclude_unset=True)

    if "amount" in changes:
        shipped, done, remaining = _remaining(
            db, record.customer, record.period, exclude_amount=float(record.amount or 0)
        )
        if changes["amount"] > remaining + EPS:
            raise HTTPException(
                400,
                f"金额 {changes['amount']:.2f} 超过该客户该期可确认余额 {remaining:.2f}"
                f"（出货 {shipped:.2f}，已确认（不含本笔）{done:.2f}）",
            )

    # 已收金额不能超过收入金额：否则「未收余额」会变成负数，
    # 看起来像客户倒欠我们钱，实际是录入错了。
    if "received_amount" in changes:
        limit = float(changes.get("amount", record.amount) or 0)
        if changes["received_amount"] > limit + EPS:
            raise HTTPException(
                400, f"已收 {changes['received_amount']:.2f} 超过收入金额 {limit:.2f}"
            )

    _labels = {
        "amount": "收入金额", "invoiced": "是否开票",
        "received_amount": "已收金额", "received_on": "收款日期", "remark": "备注",
    }
    before = {k: getattr(record, k, None) for k in changes}
    for field, value in changes.items():
        setattr(record, field, value)
    diff = audit.changes_of(
        before, {k: getattr(record, k, None) for k in changes}, _labels
    )
    if diff:
        audit.record(
            db, action="update", module="revenue",
            target_id=str(record.id),
            target_label=f"收入 {record.customer} {record.period}",
            summary=audit.describe(diff), changes=diff,
        )
    db.commit()
    db.refresh(record)
    return _to_out(record)


def withdraw_revenue(record: RevenueRecord, db: Session) -> int:
    """撤回一笔已确认的收入。

    撤回 = 删除。它本来就是从待确认里「确认」出来的，撤回后那笔钱会重新
    出现在待确认清单里，不需要留一条已撤回记录 —— 留着反而让「已确认合计」
    要处处排除它们。
    """
    record_id = record.id
    audit.record(
        db, action="cancel", module="revenue",
        target_id=str(record_id),
        target_label=f"收入 {record.customer} {record.period} ¥{record.amount:g}",
        summary=f"撤回已确认收入：{record.customer} {record.period} ¥{record.amount:g}",
    )
    db.delete(record)
    db.commit()
    return record_id


@router.get("", response_model=RevenueBoardOut)
def get_board(db: Session = Depends(get_db)):
    return board(db)


@router.post("/confirm", response_model=RevenueOut)
def confirm(req: RevenueConfirmIn, db: Session = Depends(get_db)):
    return confirm_revenue(req, db)


def get_revenue_or_404(db: Session, record_id: int) -> RevenueRecord:
    record = db.query(RevenueRecord).filter(RevenueRecord.id == record_id).first()
    if not record:
        raise HTTPException(404, f"收入记录不存在: #{record_id}")
    return record


@router.patch("/{record_id}", response_model=RevenueOut)
def update_revenue(record_id: int, req: RevenueUpdate, db: Session = Depends(get_db)):
    return update_revenue_record(get_revenue_or_404(db, record_id), req, db)


@router.delete("/{record_id}")
def withdraw(record_id: int, db: Session = Depends(get_db)):
    return {"deleted": withdraw_revenue(get_revenue_or_404(db, record_id), db)}
