from datetime import date, datetime

from pydantic import BaseModel, Field


class QuoteItemIn(BaseModel):
    seq: int = 0
    category: str = ""
    material_no: str = ""
    product: str
    model: str = ""
    quantity: int = Field(default=0, ge=0)
    unit: str = ""
    brand: str = ""
    price: float | None = Field(default=None, ge=0)
    delivery: str = ""


class QuoteImport(BaseModel):
    source_file: str = ""
    items: list[QuoteItemIn] = Field(min_length=1)


class QuoteItemUpdate(BaseModel):
    """手填字段：报价单价与交期。传 null 表示清空报价。"""

    price: float | None = Field(default=None, ge=0)
    delivery: str | None = None


class QuoteItemOut(BaseModel):
    id: int
    seq: int = 0
    category: str = ""
    material_no: str = ""
    product: str = ""
    model: str = ""
    quantity: int = 0
    unit: str = ""
    brand: str = ""
    price: float | None = None
    total_price: float | None = None
    delivery: str = ""


class QuoteSummary(BaseModel):
    id: str
    source_file: str = ""
    created_at: datetime
    item_count: int = 0
    priced_count: int = 0
    total_amount: float = 0.0


class QuoteDetail(QuoteSummary):
    items: list[QuoteItemOut] = []


class QuoteHistoryRow(BaseModel):
    order_id: str
    unit_price: float = 0.0
    accepted_date: date | None = None


class QuoteItemHistory(BaseModel):
    """一条报价明细的历史报价（物料号+品名+规格型号 三项全同、且已确认接单）。"""

    item_id: int
    material_no: str = ""
    product: str = ""
    model: str = ""
    history: list[QuoteHistoryRow] = []
