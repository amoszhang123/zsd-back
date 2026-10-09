from datetime import date, datetime

from pydantic import BaseModel, Field


class BankTransactionIn(BaseModel):
    trade_time: datetime
    income: float = Field(default=0.0, ge=0)
    expense: float = Field(default=0.0, ge=0)
    balance: float = 0.0
    counterparty_account: str = Field(default="", max_length=60)
    counterparty_name: str = Field(default="", max_length=200)
    counterparty_bank: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=100)


class BankImport(BaseModel):
    account_no: str = Field(default="", max_length=60)
    account_name: str = Field(default="", max_length=200)
    currency: str = Field(default="", max_length=20)
    period_start: date | None = None
    period_end: date | None = None
    source_file: str = Field(default="", max_length=200)
    transactions: list[BankTransactionIn] = Field(min_length=1)


class BankImportResult(BaseModel):
    parsed: int = 0
    inserted: int = 0
    skipped: int = 0
    # 全部行都是重复时不建批次，此时为 None
    statement_id: int | None = None
    account_no: str = ""
    account_name: str = ""


class BankTransactionOut(BaseModel):
    id: int
    account_no: str = ""
    trade_time: datetime
    income: float = 0.0
    expense: float = 0.0
    balance: float = 0.0
    counterparty_account: str = ""
    counterparty_name: str = ""
    counterparty_bank: str = ""
    summary: str = ""

    model_config = {"from_attributes": True}


class BankSummary(BaseModel):
    """总数。期初余额 = 最早一笔的余额 − 该笔收入 + 该笔支出（即该笔发生前的余额）。"""

    count: int = 0
    total_income: float = 0.0
    total_expense: float = 0.0
    net: float = 0.0
    opening_balance: float | None = None
    closing_balance: float | None = None


class BankTransactionPage(BaseModel):
    # summary 统计的是整个筛选范围，rows 只是当前页
    summary: BankSummary
    rows: list[BankTransactionOut] = []
    total: int = 0
    page: int = 1
    page_size: int = 50
    pages: int = 0


class BankAccountOut(BaseModel):
    account_no: str
    account_name: str = ""
    count: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None


class BankStatementOut(BaseModel):
    id: int
    account_no: str = ""
    account_name: str = ""
    currency: str = ""
    period_start: date | None = None
    period_end: date | None = None
    source_file: str = ""
    row_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}
