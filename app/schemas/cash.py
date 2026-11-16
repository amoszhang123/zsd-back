from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.models.cash import DIRECTION_IN, DIRECTION_OUT

# 预设类别。前端下拉只给这些 + 「其他」（选其他可手输），但后端不校验取值范围 ——
# 与成本页同一套做法：厂里随时会冒出「环保罚款」这种没预设过的类别，
# 卡在后端只会让人放弃录入。
PRESET_CATEGORIES_OUT = [
    "房租", "水电", "工资", "社保", "运费", "外协", "差旅", "办公", "税费", "其他",
]
PRESET_CATEGORIES_IN = ["废料", "租赁", "补贴", "利息", "其他"]


def _clean_category(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("类别不能为空")
    return cleaned


class CashEntryIn(BaseModel):
    direction: Literal[DIRECTION_IN, DIRECTION_OUT] = DIRECTION_OUT
    category: str = Field(max_length=50)
    # 必须大于 0：金额为 0 的记录没有账目意义，只会在列表里占位；
    # 方向已经表达收支，负数会让「合计为负」和「方向为支出」两种含义混在一起。
    amount: float = Field(gt=0)
    counterparty: str = Field(default="", max_length=200)
    remark: str = Field(default="", max_length=500)
    entry_date: date

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        return _clean_category(v)

    @field_validator("counterparty", "remark")
    @classmethod
    def strip_text(cls, v: str) -> str:
        return v.strip()


class CashEntryUpdate(BaseModel):
    """全部可选，未传的字段保持原值（用 exclude_unset 区分「没传」和「传了空」）。"""

    direction: Literal[DIRECTION_IN, DIRECTION_OUT] | None = None
    category: str | None = Field(default=None, max_length=50)
    amount: float | None = Field(default=None, gt=0)
    counterparty: str | None = Field(default=None, max_length=200)
    remark: str | None = Field(default=None, max_length=500)
    entry_date: date | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str | None) -> str | None:
        return None if v is None else _clean_category(v)

    @field_validator("counterparty", "remark")
    @classmethod
    def strip_text(cls, v: str | None) -> str | None:
        return None if v is None else v.strip()


class CashVoucherOut(BaseModel):
    id: int
    filename: str = ""
    size: int = 0
    content_type: str = ""
    url: str = ""
    source: str = ""
    uploaded_by: str = ""
    created_at: datetime


class CashEntryOut(BaseModel):
    id: int
    direction: str
    category: str
    amount: float = 0.0
    counterparty: str = ""
    remark: str = ""
    entry_date: date
    vouchers: list[CashVoucherOut] = []
    created_at: datetime
    updated_at: datetime
