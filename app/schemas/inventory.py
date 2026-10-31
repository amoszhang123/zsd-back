"""物料管理的请求/响应模型。

数量在库表里是 DECIMAL(12,2)，对外一律 float：前端 JS 只有 double，
返回字符串或 Decimal 都会让比较/求和变得别扭。
"""
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator


def _strip(v: str | None) -> str | None:
    return None if v is None else v.strip()


class MaterialCreate(BaseModel):
    """建档。库存固定为 0 —— 入库只有「成本记录纳入」一个入口。"""

    name: str = Field(min_length=1, max_length=100)
    spec: str = Field(default="", max_length=200)
    unit: str = Field(default="个", max_length=20)
    category: str = Field(default="", max_length=50)
    location: str = Field(default="", max_length=50)
    # 库存 < 阈值 才告警，0 表示关闭告警
    warn_threshold: float = Field(default=0, ge=0)
    remark: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def name_required(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("物料名称不能为空")
        return cleaned

    @field_validator("unit")
    @classmethod
    def unit_required(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("单位不能为空")
        return cleaned

    @field_validator("spec", "category", "location", "remark")
    @classmethod
    def strip_text(cls, v: str) -> str:
        return v.strip()


class MaterialUpdate(BaseModel):
    """全部可选，未传的保持原值。库存三列不在其中 —— 只能由流水改。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    spec: str | None = Field(default=None, max_length=200)
    unit: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, max_length=50)
    location: str | None = Field(default=None, max_length=50)
    warn_threshold: float | None = Field(default=None, ge=0)
    remark: str | None = Field(default=None, max_length=500)
    active: bool | None = None

    @field_validator("name", "unit")
    @classmethod
    def not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("不能为空")
        return cleaned

    @field_validator("spec", "category", "location", "remark")
    @classmethod
    def strip_text(cls, v: str | None) -> str | None:
        return _strip(v)


class MaterialOut(BaseModel):
    id: int
    code: str
    name: str
    spec: str = ""
    unit: str = "个"
    category: str = ""
    location: str = ""
    warn_threshold: float = 0
    active: bool = True
    stock_qty: float = 0
    issued_qty: float = 0
    scrapped_qty: float = 0
    # 入库累计 = 存量 + 报废 + 领用中，恒等式左边直接算出来给前端对账
    total_in: float = 0
    # 领用中里还没结清的部分（= 领用中 − 已还 − 已废 的合计）
    pending_qty: float = 0
    warn: bool = False
    # (阈值 − 库存) / 阈值，列表按它降序置顶
    shortage_ratio: float = 0
    remark: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MaterialSummary(BaseModel):
    material_count: int = 0
    active_count: int = 0
    warn_count: int = 0
    zero_count: int = 0
    open_issue_count: int = 0
    month_issue_count: int = 0
    month_scrap_count: int = 0


class MaterialListOut(BaseModel):
    rows: list[MaterialOut]
    summary: MaterialSummary


class MovementOut(BaseModel):
    id: int
    material_id: int
    material_code: str = ""
    material_name: str = ""
    mtype: str
    mtype_label: str = ""
    qty: float = 0
    stock_before: float = 0
    stock_after: float = 0
    issued_before: float = 0
    issued_after: float = 0
    scrapped_before: float = 0
    scrapped_after: float = 0
    ref_type: str = ""
    ref_id: int | None = None
    ref_no: str = ""
    employee_id: str | None = None
    employee_name: str = ""
    remark: str = ""
    created_at: datetime


class StockInRequest(BaseModel):
    """把一条成本记录纳入库存。

    material_id 与 new_material 二选一：选已有物料就合并，现场新建就顺手建物料。
    不做「按类别+规格自动匹配」—— 一个空格、全半角差异就会分裂成两个物料，
    库存被摊薄、告警也失真。
    """

    cost_record_id: int
    material_id: int | None = None
    new_material: MaterialCreate | None = None
    # 默认取成本记录的 quantity；给了就以给的为准（单位折算用）
    quantity: float | None = Field(default=None, gt=0)
    remark: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def exactly_one_target(self):
        if (self.material_id is None) == (self.new_material is None):
            raise ValueError("material_id 与 new_material 必须且只能提供一个")
        return self


class IssueItemIn(BaseModel):
    material_id: int
    qty: float = Field(gt=0)


class IssueCreate(BaseModel):
    employee_id: str = Field(min_length=1)
    items: list[IssueItemIn] = Field(min_length=1)
    remark: str = Field(default="", max_length=500)


class IssueItemOut(BaseModel):
    id: int
    material_id: int
    material_code: str = ""
    material_name: str = ""
    unit: str = ""
    qty: float = 0
    returned_qty: float = 0
    scrapped_qty: float = 0
    # 还没还也没废的数量，归还弹窗按它限上限
    open_qty: float = 0


class IssueOrderOut(BaseModel):
    id: int
    order_no: str
    employee_id: str
    employee_name: str = ""
    status: str
    status_label: str = ""
    items_summary: str = ""
    remark: str = ""
    issued_at: datetime
    returned_at: datetime | None = None
    items: list[IssueItemOut] = []


class ReturnItemIn(BaseModel):
    item_id: int
    return_qty: float = Field(default=0, ge=0)
    scrap_qty: float = Field(default=0, ge=0)


class ReturnRequest(BaseModel):
    """归还/报废一次提交。同一行可以一半还一半废，两项之和不得超过未结清数量。"""

    items: list[ReturnItemIn] = Field(min_length=1)
    # 有报废时必填 —— 报废单要写清原因，否则月底对不上账没人说得清
    reason: str = Field(default="", max_length=500)


class StockScrapRequest(BaseModel):
    """仓管对库存直接报废（料锈了/过期了）。存量−、报废+，恒等式不变。"""

    material_id: int
    qty: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class ScrapItemOut(BaseModel):
    id: int
    material_id: int
    material_code: str = ""
    material_name: str = ""
    unit: str = ""
    qty: float = 0


class ScrapOrderOut(BaseModel):
    id: int
    order_no: str
    source: str
    source_label: str = ""
    issue_order_id: int | None = None
    issue_order_no: str = ""
    employee_id: str | None = None
    employee_name: str = ""
    reason: str = ""
    created_at: datetime
    items: list[ScrapItemOut] = []


class LabelPagesOut(BaseModel):
    pages: list[str]  # data:image/png;base64,...


class LabelRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
