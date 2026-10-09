from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.bank import BankStatement, BankTransaction
from app.schemas.bank import (
    BankAccountOut,
    BankImport,
    BankImportResult,
    BankStatementOut,
    BankSummary,
    BankTransactionOut,
    BankTransactionPage,
)

router = APIRouter(prefix="/api/bank", tags=["bank"])


def _money_key(value: float) -> float:
    """判重用的金额键：统一取 2 位小数，避免浮点尾差让同一笔被判成两笔。"""
    return round(float(value or 0.0), 2)


def _time_key(value: datetime) -> datetime:
    """MySQL DATETIME 只到秒，判重前把微秒抹掉，否则带毫秒的流水永远判不出重复。"""
    return value.replace(microsecond=0)


def _natural_key(tx) -> tuple:
    """单账户内的自然键（账号已在查询层过滤，不进键）。"""
    return (
        _time_key(tx.trade_time),
        _money_key(tx.income),
        _money_key(tx.expense),
        _money_key(tx.balance),
        tx.counterparty_account.strip(),
    )


def _summary_from_query(query) -> BankSummary:
    """按筛选条件用 SQL 聚合算总数 —— 必须是全量而非当前页，否则分页后总数就错了。

    银行流水的「账户余额」是该笔发生后的余额，
    所以期初余额 = 最早一笔的余额 − 该笔收入 + 该笔支出。
    """
    count, income, expense = query.with_entities(
        func.count(BankTransaction.id),
        func.coalesce(func.sum(BankTransaction.income), 0.0),
        func.coalesce(func.sum(BankTransaction.expense), 0.0),
    ).one()
    if not count:
        return BankSummary()

    income = round(float(income), 2)
    expense = round(float(expense), 2)
    first = query.order_by(BankTransaction.trade_time.asc(), BankTransaction.id.asc()).first()
    last = query.order_by(BankTransaction.trade_time.desc(), BankTransaction.id.desc()).first()
    return BankSummary(
        count=count,
        total_income=income,
        total_expense=expense,
        net=round(income - expense, 2),
        opening_balance=round(first.balance - first.income + first.expense, 2),
        closing_balance=round(last.balance, 2),
    )


@router.post("/import", response_model=BankImportResult)
def import_statement(payload: BankImport, db: Session = Depends(get_db)):
    account_no = payload.account_no.strip()
    if not account_no:
        raise HTTPException(400, "缺少账号，无法归集到具体银行账户")

    parsed = len(payload.transactions)
    existing = {
        (
            _time_key(row[0]),
            _money_key(row[1]),
            _money_key(row[2]),
            _money_key(row[3]),
            (row[4] or "").strip(),
        )
        for row in db.query(
            BankTransaction.trade_time,
            BankTransaction.income,
            BankTransaction.expense,
            BankTransaction.balance,
            BankTransaction.counterparty_account,
        )
        .filter(BankTransaction.account_no == account_no)
        .all()
    }

    fresh = []
    skipped = 0
    for tx in payload.transactions:
        key = _natural_key(tx)
        if key in existing:
            skipped += 1
            continue
        # 同一份文件里也可能有两行完全相同，一并去重
        existing.add(key)
        fresh.append(tx)

    if not fresh:
        return BankImportResult(
            parsed=parsed,
            inserted=0,
            skipped=skipped,
            statement_id=None,
            account_no=account_no,
            account_name=payload.account_name.strip(),
        )

    statement = BankStatement(
        account_no=account_no,
        account_name=payload.account_name.strip(),
        currency=payload.currency.strip(),
        period_start=payload.period_start,
        period_end=payload.period_end,
        source_file=payload.source_file.strip(),
        row_count=len(fresh),
    )
    db.add(statement)
    db.flush()

    for tx in fresh:
        db.add(
            BankTransaction(
                statement_id=statement.id,
                account_no=account_no,
                trade_time=_time_key(tx.trade_time),
                income=_money_key(tx.income),
                expense=_money_key(tx.expense),
                balance=_money_key(tx.balance),
                counterparty_account=tx.counterparty_account.strip(),
                counterparty_name=tx.counterparty_name.strip(),
                counterparty_bank=tx.counterparty_bank.strip(),
                summary=tx.summary.strip(),
            )
        )
    db.commit()

    return BankImportResult(
        parsed=parsed,
        inserted=len(fresh),
        skipped=skipped,
        statement_id=statement.id,
        account_no=account_no,
        account_name=statement.account_name,
    )


@router.get("/transactions", response_model=BankTransactionPage)
def list_transactions(
    month: str | None = None,
    year: int | None = None,
    account_no: str | None = None,
    keyword: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """按月或按年分页查询流水。

    summary 里的笔数与各项合计始终按整个筛选范围统计（SQL 聚合），
    只有 rows 分页 —— 否则翻页会让「总数」跟着变。
    """
    query = db.query(BankTransaction)

    if month:
        try:
            start = datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise HTTPException(400, f"月份格式应为 YYYY-MM，收到: {month}")
        end = datetime(start.year + 1, 1, 1) if start.month == 12 else datetime(
            start.year, start.month + 1, 1
        )
        query = query.filter(BankTransaction.trade_time >= start, BankTransaction.trade_time < end)
    elif year:
        query = query.filter(
            BankTransaction.trade_time >= datetime(year, 1, 1),
            BankTransaction.trade_time < datetime(year + 1, 1, 1),
        )

    if account_no:
        query = query.filter(BankTransaction.account_no == account_no.strip())
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        query = query.filter(
            BankTransaction.counterparty_name.like(like)
            | BankTransaction.counterparty_account.like(like)
            | BankTransaction.counterparty_bank.like(like)
            | BankTransaction.summary.like(like)
        )

    summary = _summary_from_query(query)
    total = summary.count
    pages = (total + page_size - 1) // page_size
    # 页码超出范围时收敛到最后一页，而不是返回一个空列表让前端以为没数据
    current = min(page, pages) if pages else 1

    rows = (
        query.order_by(BankTransaction.trade_time.desc(), BankTransaction.id.desc())
        .offset((current - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return BankTransactionPage(
        summary=summary,
        rows=[BankTransactionOut.model_validate(r) for r in rows],
        total=total,
        page=current,
        page_size=page_size,
        pages=pages,
    )


@router.get("/accounts", response_model=list[BankAccountOut])
def list_accounts(db: Session = Depends(get_db)):
    """已导入过流水的银行账户，供筛选下拉使用。"""
    rows = (
        db.query(
            BankTransaction.account_no,
            func.count(BankTransaction.id),
            func.min(BankTransaction.trade_time),
            func.max(BankTransaction.trade_time),
        )
        .group_by(BankTransaction.account_no)
        .order_by(BankTransaction.account_no)
        .all()
    )
    names = {s.account_no: s.account_name for s in db.query(BankStatement).all()}
    return [
        BankAccountOut(
            account_no=row[0],
            account_name=names.get(row[0], ""),
            count=row[1],
            earliest=row[2],
            latest=row[3],
        )
        for row in rows
    ]


@router.get("/statements", response_model=list[BankStatementOut])
def list_statements(db: Session = Depends(get_db)):
    return (
        db.query(BankStatement)
        .order_by(BankStatement.created_at.desc(), BankStatement.id.desc())
        .all()
    )


@router.delete("/statements/{statement_id}")
def delete_statement(statement_id: int, db: Session = Depends(get_db)):
    """删除一整批导入（导错文件的唯一纠错手段）。单条流水不可改也不可删。"""
    statement = db.get(BankStatement, statement_id)
    if not statement:
        raise HTTPException(404, f"导入批次不存在: #{statement_id}")
    deleted = len(statement.transactions)
    db.delete(statement)
    db.commit()
    return {"deleted": statement_id, "transactions_removed": deleted}
