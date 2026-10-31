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
    # 所属合同的状态（正常/暂停/取消/结案）。非「正常」时该工单不能接单，前端据此置灰。
    contract_status: str = ""
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
    # 必填且不能是空串。web 与小程序的表单都在前端拦了，但直接调接口能绕过，
    # 而一张没有品名的工单在列表、送货单、扫码解析里全是「—」，等于废数据。
    product: str = Field(min_length=1)
    model: str = ""
    # 不能为负：下游的累计完工数、质检待检数量都拿它当基数，负数会算出负的待检量。
    quantity: int = Field(default=0, ge=0)
    # 不再接受 estimated_hours：预估工时只能由程序阶段填的加工时长算出
    priority: str = "普通"


class OrderUpdate(BaseModel):
    order_no: str | None = None
    project_no: str | None = None
    material_no: str | None = None
    # None 表示「这次不改这个字段」（update_order 用 exclude_unset），
    # 所以约束只在真的传了值时生效：传空串 / 负数一样要被挡下来。
    product: str | None = Field(default=None, min_length=1)
    model: str | None = None
    quantity: int | None = Field(default=None, ge=0)
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
    # 整批一个订单类型。目标阶段由类型 + skip_material 共同决定，
    # 见 batch_confirm_target_step。
    order_type: Literal["自主", "全外协", "半外协"] = "自主"
    # 默认 True：跳过备料，工单停在程序阶段（备料标记为已完成）。
    # False：不跳过，停在备料阶段，与单张接单一致。
    # 前端两处（web OrderList / 小程序 orderIntake）都是默认勾选。
    # 全外协忽略此参数，恒进质检1。
    skip_material: bool = True


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


class EmployeePerfRow(BaseModel):
    """员工效能看板的一行。

    hours       投产工时 = Σ(订单预估工时 × 该员工参与度)，订单以「质检1首次合格」的月份归属
    order_count 计入工时的订单数
    output_qty  当月完工产出件数（该员工参与的已完工开工单明细合计）
    defect_qty  当月记录的返工件数（返工 + 报废 + 维修，按异常单负责人归属）
    good_rate   良品率 = (产出 − 不良) / 产出，无产出时为 None（前端显示「—」）
    """

    employee_id: str
    employee_name: str = ""
    position: str = ""
    employee_type: str = ""
    status: str = ""
    hours: float = 0.0
    order_count: int = 0
    output_qty: int = 0
    defect_qty: int = 0
    rework_qty: int = 0
    scrap_qty: int = 0
    repair_qty: int = 0
    good_rate: float | None = None
    # 占本月全员投产工时的比例，前端画条形图用
    hours_share: float = 0.0


class EmployeePerfBoard(BaseModel):
    month: str
    rows: list[EmployeePerfRow] = []
    total_hours: float = 0.0
    total_output: int = 0
    total_defect: int = 0
    good_rate: float | None = None
    employee_count: int = 0
    order_count: int = 0
    # 本月质检1合格、但预估工时没填（为 0）的订单数 —— 这些订单对工时贡献为 0，
    # 要在看板上明说，否则用户会以为工时统计少算了。
    missing_hours_orders: int = 0
