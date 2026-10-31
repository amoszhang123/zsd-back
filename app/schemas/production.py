from datetime import date, datetime

from pydantic import BaseModel, Field


class StartWorkRequest(BaseModel):
    employee_ids: list[str] = Field(min_length=1)
    order_ids: list[str] = Field(min_length=1)
    # order_id → machine_id。选填：web 端可以逐行选机台，也可以整张单不选。
    # 老链路（小程序直接扫订单码开工）完全不传这个字段，机台留空。
    machine_map: dict[str, str] = {}


class WorkOrderEmployeeOut(BaseModel):
    employee_id: str
    employee_name: str = ""


class WorkOrderItemOut(BaseModel):
    order_id: str
    product: str = ""
    quantity: int = 0
    completed_qty: int | None = None
    # 加工机台（订单行级）。为空 = 走老链路开工，没关联机台。
    machine_id: str | None = None
    machine_name: str = ""


class WorkOrderResponse(BaseModel):
    id: int
    work_date: date
    status: str = "开工"
    employees: list[WorkOrderEmployeeOut] = []
    items: list[WorkOrderItemOut] = []
    # 本单涉及的机台（去重，按首次出现顺序），列表页一列展示用。
    # 一单可跨机台，所以这里给的是集合，不是单个值。
    machines: list[str] = []
    completed_qty_total: int = 0
    created_at: datetime
    completed_at: datetime | None = None


class StartWorkResponse(BaseModel):
    success: bool
    message: str
    work_order: WorkOrderResponse | None = None


class ContributionIn(BaseModel):
    employee_id: str
    percent: int = Field(ge=0, le=100)


class CompleteWorkItem(BaseModel):
    order_id: str
    # 允许 0：接力生产时，本次没做完的订单就填 0（或干脆不提交，后端按 0 处理）
    completed_qty: int = Field(default=0, ge=0)
    # 本次完工使该订单达到全量完成、且参与员工多于 1 人时必填：
    # 必须覆盖全部参与员工，percent 合计须为 100
    contributions: list[ContributionIn] | None = None


class CompleteWorkRequest(BaseModel):
    work_order_id: int
    employee_id: str
    items: list[CompleteWorkItem] = Field(min_length=1)


class CompleteWorkResponse(BaseModel):
    success: bool
    message: str
    work_order: WorkOrderResponse


class OrderEmployeeOut(BaseModel):
    employee_id: str
    employee_name: str = ""
    # 参与度百分比；订单尚未全量完成时为 None
    percent: int | None = None
