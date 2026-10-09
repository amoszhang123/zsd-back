from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Stage = Literal["quality1", "surface", "quality2"]
DispositionType = Literal["rework", "scrap", "repair"]
DispositionStatus = Literal["待处理", "处理中", "已完成"]


class OwnerIn(BaseModel):
    """单据负责人（可多选）+ 逐人工时与金额。

    金额的含义随单据类型不同：报废单是「物料损失」，返工/维修单是「额外支出」。
    同一列存，界面按类型显示不同名称。选填、默认 0。
    """

    employee_id: str
    labor_hours: float = Field(default=0.0, ge=0)
    material_cost: float = Field(default=0.0, ge=0)


class OwnerOut(BaseModel):
    employee_id: str
    employee_name: str = ""
    labor_hours: float = 0.0
    material_cost: float = 0.0
    updated_at: datetime | None = None


class DispositionLossUpdate(BaseModel):
    """修改单据的工时与金额（报废=物料损失，返工/维修=额外支出）。

    只允许改已在名单上的负责人，不能用这个接口增删负责人；传谁就改谁，没传的保持原值。
    """

    owners: list[OwnerIn] = Field(min_length=1)


class DispositionCreate(BaseModel):
    order_id: str
    quantity: int = Field(gt=0)
    employee_id: str | None = None
    stage: Stage
    owners: list[OwnerIn] = []


class DispositionStatusUpdate(BaseModel):
    status: DispositionStatus


class DispositionOut(BaseModel):
    id: int
    type: DispositionType
    order_id: str
    product: str = ""
    quantity: int
    employee_id: str | None = None
    employee_name: str = ""
    stage: Stage
    status: DispositionStatus | None = None
    created_at: datetime
    updated_at: datetime | None = None
    owners: list[OwnerOut] = []
    total_labor_hours: float = 0.0
    total_material_cost: float = 0.0


class QualityResultRequest(BaseModel):
    """质检结果：按 stage 分行存储，字段可选，只更新传入的部分。

    stage 决定写入哪一行（quality1 首检 / surface 表处 / quality2 终检），各阶段互不覆盖。
    """

    stage: Literal["quality1", "surface", "quality2"] = "quality1"
    pass_count: int | None = Field(default=None, ge=0)
    fail_count: int | None = Field(default=None, ge=0)
    qc_status: Literal["pass", "rework", "scrap", "repair"] | None = None
    remark: str | None = None


class QcJudgmentRequest(BaseModel):
    """统一质检判定：先定报废数量，再对剩余部分判定合格/返工/维修。

    quantity 为本次送检数量，缺省表示把该阶段全部待检数量一次检完（支持部分质检）。
    返工与维修都是整批打回（订单退回投产阶段），只有报废需要单独记数量。
    报废数量等于送检数量时视为整批报废，不再产生返工/维修单。
    """

    order_id: str
    stage: Literal["quality1", "surface", "quality2"]
    quantity: int | None = Field(default=None, gt=0)
    scrap_qty: int = Field(default=0, ge=0)
    verdict: Literal["pass", "rework", "repair"] = "pass"
    employee_id: str | None = None
    # 实际做这次质检的人，与 employee_id（第一位负责人 / 损耗归属）不是一回事。
    # 小程序端由后端从登录态强制填入，不接受客户端传值（否则能替别人记功）。
    inspector_id: str | None = None
    remark: str = ""
    # 负责人（多选）。同一次判定产生的单据共用这份名单。
    # owners 同时承载「报废单」的工时与物料损失；
    # return_owners 承载「返工/维修单」的工时与额外支出 —— 一次判定既报废又返工时
    # 两张单各填各的，避免同一份工时被重复计入员工头上。
    # return_owners 为空时回退用 owners 的名单、损耗记 0（兼容只传 owners 的旧调用方）。
    owners: list[OwnerIn] = []
    return_owners: list[OwnerIn] = []


class QcJudgmentResponse(BaseModel):
    success: bool
    message: str
    qc_status: str
    inspected_qty: int = 0
    scrap_qty: int = 0
    passed_qty: int = 0
    rejected_qty: int = 0
    pending_qty: int = 0
    returned_to_production: bool = False
    created_docs: list[str] = []


class QcQueueItem(BaseModel):
    """某质检阶段的待检队列行。待检数量 = 上游产出 − 本阶段已检。"""

    order_id: str
    product: str = ""
    customer: str = ""
    project_no: str = ""
    material_no: str = ""
    model: str = ""
    deadline: date | None = None
    quantity: int = 0
    current_step: int = 0
    order_status: str = ""
    upstream_qty: int = 0
    inspected_qty: int = 0
    pending_qty: int = 0
    pass_qty: int = 0
    scrap_qty: int = 0
    batch_count: int = 0
    last_verdict: str = ""


class QcInspectionOut(BaseModel):
    """一次质检批次的记录。"""

    id: int
    order_id: str
    stage: str
    quantity: int = 0
    pass_count: int = 0
    fail_count: int = 0
    scrap_qty: int = 0
    verdict: str = "pass"
    remark: str = ""
    employee_id: str | None = None
    employee_name: str = ""
    inspector_id: str | None = None
    inspector_name: str = ""
    created_at: datetime
