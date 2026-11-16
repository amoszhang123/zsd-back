"""经营管理概览：把一个月的收入与支出汇总成三个数。

放在独立路由而不是塞进 dashboard：仪表盘是「生产进度」视角（工单、交期、质检），
这里是「钱」的视角，混在一起会让两边都改不动。
"""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db, scoped
from app.models.cash import DIRECTION_IN, DIRECTION_OUT, CashEntry
from app.models.cost import CostRecord
from app.models.revenue import RevenueRecord
from app.schemas.overview import OverviewOut
from app.schemas.revenue import PERIOD_RE

router = APIRouter(prefix="/api/overview", tags=["overview"])


def _month_range(month: str) -> tuple[date, date]:
    """把 "2026-11" 转成 [月初, 下月初)。用半开区间，避免 23:59:59 那类边界漏行。

    日期列是 DATE（没有时分秒），但半开区间对 DATE 和 DATETIME 都正确，
    以后哪张表换成 DATETIME 也不用改这里。

    格式用 PERIOD_RE 严格校验，不靠 int() 兜底：`int("1")` 是合法的，
    "2026-1" 会被悄悄当成 1 月，前端传错月份时这里就成了「默默给一个别的月」。
    """
    if not PERIOD_RE.match((month or "").strip()):
        raise HTTPException(400, "月份格式应为 YYYY-MM，例如 2026-11")
    y, m = (int(x) for x in month.strip().split("-"))
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start, end


@router.get("", response_model=OverviewOut)
def get_overview(
    month: str = Query(default="", description="YYYY-MM，留空表示本月"),
    db: Session = Depends(get_db),
):
    if not month:
        month = datetime.now().strftime("%Y-%m")
    start, end = _month_range(month)

    # —— 收入 ——
    # 订单收入按 period（"YYYY-MM"）等值取，它就是按出货日期归好的月份；
    # 这里再用日期范围过滤反而会因为 ship_date 与确认时点的差异而不一致。
    order_revenue = float(
        db.query(func.sum(RevenueRecord.amount))
        .filter(
            scoped(db, RevenueRecord),
            RevenueRecord.period == month,
        )
        .scalar()
        or 0
    )

    # 费用收支按发生日期 entry_date 归月（不是录入时间）：补录上月的水电费
    # 要算在上月，否则「本月支出」会因录入时间而虚高。
    other_income = float(
        db.query(func.sum(CashEntry.amount))
        .filter(
            scoped(db, CashEntry),
            CashEntry.direction == DIRECTION_IN,
            CashEntry.entry_date >= start,
            CashEntry.entry_date < end,
        )
        .scalar()
        or 0
    )
    cash_out = float(
        db.query(func.sum(CashEntry.amount))
        .filter(
            scoped(db, CashEntry),
            CashEntry.direction == DIRECTION_OUT,
            CashEntry.entry_date >= start,
            CashEntry.entry_date < end,
        )
        .scalar()
        or 0
    )

    # 成本支出：金额不落库，一律 数量 × 单价 实时算，与成本页的合计口径一致。
    cost = float(
        db.query(
            func.sum(
                func.coalesce(CostRecord.quantity, 0) * func.coalesce(CostRecord.unit_price, 0)
            )
        )
        .filter(
            scoped(db, CostRecord),
            CostRecord.purchase_date >= start,
            CostRecord.purchase_date < end,
        )
        .scalar()
        or 0
    )

    income = round(order_revenue + other_income, 2)
    expense = round(cost + cash_out, 2)
    return OverviewOut(
        month=month,
        order_revenue=round(order_revenue, 2),
        other_income=round(other_income, 2),
        income=income,
        cost=round(cost, 2),
        cash_out=round(cash_out, 2),
        expense=expense,
        profit=round(income - expense, 2),
    )
