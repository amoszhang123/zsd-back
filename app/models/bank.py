from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Double,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BankStatement(Base):
    """一次银行流水导入（一个批次）。

    流水是银行事实、不允许改；导错文件的纠错方式是删除整批后重导。
    """

    __tablename__ = "bank_statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_no: Mapped[str] = mapped_column(String(60), index=True, default="")
    account_name: Mapped[str] = mapped_column(String(200), default="")
    currency: Mapped[str] = mapped_column(String(20), default="")
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_file: Mapped[str] = mapped_column(String(200), default="")
    # 本批实际入库笔数（已跳过的重复行不计入）
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    transactions: Mapped[list["BankTransaction"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )


class BankTransaction(Base):
    """银行流水单笔，栏目与银行导出的「账户明细」一一对应。

    金额列用 NOT NULL DEFAULT 0 而不是 NULL：MySQL 唯一索引把 NULL 视为互不相同，
    若允许 NULL，收入/支出必有一列为空，判重约束就会完全失效。

    金额用 Double（MySQL DOUBLE，8 字节）而不是默认的 Float：默认 Float 建出来是单精度
    FLOAT，198133.87 会被存成 198134.0，银行余额差 0.13 是不可接受的。
    """

    __tablename__ = "bank_transactions"
    __table_args__ = (
        UniqueConstraint(
            "account_no",
            "trade_time",
            "income",
            "expense",
            "balance",
            "counterparty_account",
            name="uq_bank_tx_natural_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    statement_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bank_statements.id"), index=True
    )
    # 账号冗余存在流水行上：多账户筛选和判重都不必回连批次表
    account_no: Mapped[str] = mapped_column(String(60), index=True, default="")
    trade_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    income: Mapped[float] = mapped_column(Double, default=0.0)
    expense: Mapped[float] = mapped_column(Double, default=0.0)
    balance: Mapped[float] = mapped_column(Double, default=0.0)
    counterparty_account: Mapped[str] = mapped_column(String(60), default="")
    counterparty_name: Mapped[str] = mapped_column(String(200), default="")
    counterparty_bank: Mapped[str] = mapped_column(String(300), default="")
    summary: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    statement: Mapped["BankStatement"] = relationship(back_populates="transactions")
