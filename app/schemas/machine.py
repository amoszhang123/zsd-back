"""机台主数据的出入参。

编号（id）只读、由后端生成；能改的只有名称、型号、启停用。
"""
from datetime import date, datetime

from pydantic import BaseModel, Field


class MachineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    model: str = Field(default="", max_length=50)


class MachineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    model: str | None = Field(default=None, max_length=50)
    active: bool | None = None


class MachineOut(BaseModel):
    id: str
    name: str
    model: str = ""
    active: bool = True
    created_at: datetime
    # 列表页要能看出「这台机器有没有在用」，但不想为每行单独查一次，
    # 由列表接口一次 group by 填好
    work_order_count: int = 0
    order_count: int = 0
    last_used_at: datetime | None = None


class MachineSummary(BaseModel):
    total: int = 0
    active: int = 0
    inactive: int = 0
    # 当前有未完工开工单挂在上面的机台数
    busy: int = 0


class MachineListOut(BaseModel):
    rows: list[MachineOut] = []
    summary: MachineSummary = MachineSummary()


class MachineHistoryItem(BaseModel):
    """这台机台干过的一行活：某张开工单里的某个订单。"""

    work_order_id: int
    work_date: date
    status: str
    order_id: str
    product: str = ""
    quantity: int = 0
    completed_qty: int | None = None
    # 该开工单的负责人，逗号分隔
    employees: str = ""
    created_at: datetime


class LabelPagesOut(BaseModel):
    """100×70 热敏一页一张，前端按页插入分页符连着打。"""

    pages: list[str] = []


class MachineLabelRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
