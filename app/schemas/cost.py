from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

# 前端预设类别；选「其他」时由用户手输，故后端不强制校验取值范围
PRESET_CATEGORIES = ["刀具", "油品", "材料", "劳保", "备品", "工具", "其他"]


def _clean_category(value: str) -> str:
    # 必须去空格：否则「刀具」和「刀具 」会被按类别统计当成两类
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("类别不能为空")
    return cleaned


class CostRecordIn(BaseModel):
    category: str = Field(max_length=50)
    spec: str = Field(default="", max_length=200)
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    supplier: str = Field(default="", max_length=200)
    invoiced: bool = False
    remark: str = Field(default="", max_length=500)
    purchase_date: date

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        return _clean_category(v)

    @field_validator("spec", "supplier", "remark")
    @classmethod
    def strip_text(cls, v: str) -> str:
        return v.strip()


class CostRecordUpdate(BaseModel):
    """全部可选，未传的字段保持原值（用 exclude_unset 区分「没传」和「传了空」）。"""

    category: str | None = Field(default=None, max_length=50)
    spec: str | None = Field(default=None, max_length=200)
    quantity: float | None = Field(default=None, gt=0)
    unit_price: float | None = Field(default=None, ge=0)
    supplier: str | None = Field(default=None, max_length=200)
    invoiced: bool | None = None
    remark: str | None = Field(default=None, max_length=500)
    purchase_date: date | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str | None) -> str | None:
        return None if v is None else _clean_category(v)

    @field_validator("spec", "supplier", "remark")
    @classmethod
    def strip_text(cls, v: str | None) -> str | None:
        return None if v is None else v.strip()


class CostRecordOut(BaseModel):
    id: int
    category: str
    spec: str = ""
    quantity: float = 0.0
    unit_price: float = 0.0
    amount: float = 0.0
    supplier: str = ""
    invoiced: bool = False
    remark: str = ""
    purchase_date: date
    created_at: datetime
    updated_at: datetime
