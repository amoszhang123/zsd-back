from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class StepSchema(BaseModel):
    key: str
    label: str
    status: str
    operator: str | None = None
    time: date | None = None


class MaterialSchema(BaseModel):
    name: str
    stock: int = 0
    required: int = 0
    status: str = "ready"


class QualityResultSchema(BaseModel):
    stage: str = "quality1"
    pass_count: int = 0
    fail_count: int = 0
    rate: float = 0
    qc_status: str = "pass"
    remark: str = ""


class OrderResponse(BaseModel):
    id: str
    order_no: str = ""
    project_no: str = ""
    material_no: str = ""
    customer: str = ""
    product: str = ""
    model: str = ""
    quantity: int = 0
    estimated_hours: float = 0
    # 加工时长（分钟），只在程序阶段填写；None 表示尚未填写
    machining_minutes: float | None = None
    priority: str = "普通"
    order_type: str = "自主"
    current_step: int = 0
    status: str = ""
    contract_id: str = ""
    create_date: date | None = None
    deadline: date | None = None
    steps: list[StepSchema] = []
    materials: list[MaterialSchema] = []
    quality_results: list[QualityResultSchema] = []

    model_config = {"from_attributes": True}


class OrderCreate(BaseModel):
    order_no: str = ""
    project_no: str = ""
    material_no: str = ""
    product: str
    model: str = ""
    quantity: int = 0
    # 不再接受 estimated_hours：预估工时只能由程序阶段填的加工时长算出
    priority: str = "普通"


class OrderUpdate(BaseModel):
    order_no: str | None = None
    project_no: str | None = None
    material_no: str | None = None
    product: str | None = None
    model: str | None = None
    quantity: int | None = None
    # 同上，预估工时不从这里改
    priority: str | None = None


class MachiningUpdate(BaseModel):
    """填写加工时长（分钟）。预估工时由后端按 ÷60÷0.8 算出，不接受前端传入。"""

    machining_minutes: float = Field(ge=0, le=10_000_000)


class OrderImportRow(BaseModel):
    material_no: str = ""
    product: str
    model: str = ""
    quantity: int = 0


class BatchConfirmRequest(BaseModel):
    order_ids: list[str] = Field(min_length=1)
    # 整批一个类型：自主/半外协 → 投产中，全外协 → 质检中。
    # 原来的 skip_to_production 已移除，目标阶段完全由类型决定。
    order_type: Literal["自主", "全外协", "半外协"] = "自主"


class BatchConfirmSkipped(BaseModel):
    order_id: str
    reason: str


class BatchConfirmResponse(BaseModel):
    success: bool
    message: str
    target_status: str
    confirmed: list[str] = []
    skipped: list[BatchConfirmSkipped] = []
    missing: list[str] = []


class DashboardStats(BaseModel):
    today_orders: int = 0
    in_production: int = 0
    today_ship: int = 0
    # 没有任何质检批次时为 None，前端显示「—」，不要用假默认值冒充良品率
    quality_rate: float | None = None
    quality_sample: int = 0
    alert_count: int = 0
    completion_rate: float = 0.0


class DashboardAlert(BaseModel):
    """工作台异常预警条目。

    kind: overdue(超期) / due_soon(临期) / pending_doc(待处理返工维修单) / shortage(缺料)
    level: danger / warning，决定前端配色
    """

    kind: str
    level: str
    title: str
    detail: str = ""
    order_id: str | None = None
    doc_type: str | None = None
    doc_id: int | None = None
