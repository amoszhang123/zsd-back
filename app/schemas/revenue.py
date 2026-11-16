import re
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

# "2026-10"。用正则而不是 date 解析：期间是「月」，不是某一天，
# 拆成 date 反而要造一个"每月 1 号"的伪日期，查询和展示都得再转回来。
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _clean_period(value: str) -> str:
    cleaned = (value or "").strip()
    if not PERIOD_RE.match(cleaned):
        raise ValueError("月份格式应为 YYYY-MM，例如 2026-10")
    return cleaned


class RevenueConfirmIn(BaseModel):
    """把某客户某月的待确认出货额确认成收入。"""

    customer: str = Field(max_length=200)
    period: str
    amount: float = Field(gt=0)
    remark: str = Field(default="", max_length=500)

    @field_validator("period")
    @classmethod
    def validate_period(cls, v: str) -> str:
        return _clean_period(v)

    @field_validator("customer")
    @classmethod
    def validate_customer(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("客户不能为空")
        return cleaned

    @field_validator("remark")
    @classmethod
    def strip_text(cls, v: str) -> str:
        return v.strip()


class RevenueUpdate(BaseModel):
    """改已确认的收入。

    amount 可改，但会被重新校验「该客户该月累计确认不能超过出货总额」；
    真要突破这个上限（比如出货单被删了），先撤回再重确认。
    """

    amount: float | None = Field(default=None, gt=0)
    invoiced: bool | None = None
    received_amount: float | None = Field(default=None, ge=0)
    received_on: date | None = None
    remark: str | None = Field(default=None, max_length=500)

    @field_validator("remark")
    @classmethod
    def strip_text(cls, v: str | None) -> str | None:
        return None if v is None else v.strip()


class RevenueOut(BaseModel):
    id: int
    customer: str
    period: str
    amount: float = 0.0
    shipped_amount: float = 0.0
    invoiced: bool = False
    received_amount: float = 0.0
    received_on: date | None = None
    # 未收余额，后端算好给前端：前端各页再算一遍容易出现「有些页忘了扣」的不一致
    outstanding: float = 0.0
    remark: str = ""
    confirmed_at: datetime
    created_at: datetime
    updated_at: datetime


class PendingRevenueOut(BaseModel):
    """待确认：某客户某月还有多少钱没确认。

    shipped_amount 是当期出货总额，confirmed_amount 是已经确认过的（可能有多笔，
    含补录产生的差额记录），remaining 是还差多少。
    三者满足 shipped = confirmed + remaining，任何时点都对得上。
    """

    customer: str
    period: str
    shipped_amount: float = 0.0
    confirmed_amount: float = 0.0
    remaining: float = 0.0


class RevenueBoardOut(BaseModel):
    pending: list[PendingRevenueOut] = []
    confirmed: list[RevenueOut] = []
