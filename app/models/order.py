from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import (
    String, Integer, Double, Date, DateTime, Text, ForeignKey, UniqueConstraint,
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from app.database import Base


class StepStatus(str, enum.Enum):
    waiting = "waiting"
    processing = "processing"
    completed = "completed"
    rejected = "rejected"


# 订单类型：接单时选定，接单后不可修改
ORDER_TYPE_SELF = "自主"
ORDER_TYPE_FULL_OUTSOURCE = "全外协"
ORDER_TYPE_HALF_OUTSOURCE = "半外协"
ORDER_TYPES = (ORDER_TYPE_SELF, ORDER_TYPE_FULL_OUTSOURCE, ORDER_TYPE_HALF_OUTSOURCE)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    order_no: Mapped[str] = mapped_column(String(50), index=True, default="")
    project_no: Mapped[str] = mapped_column(String(50), index=True, default="")
    material_no: Mapped[str] = mapped_column(String(50), default="")
    customer: Mapped[str] = mapped_column(String(100), default="")
    product: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    estimated_hours: Mapped[float] = mapped_column(Double, default=0)
    # 加工时长（分钟），只在「程序」阶段填写；NULL 表示尚未填写（与填 0 区分）
    machining_minutes: Mapped[float | None] = mapped_column(Double, nullable=True)
    priority: Mapped[str] = mapped_column(String(10), default="普通")
    order_type: Mapped[str] = mapped_column(
        String(20), default=ORDER_TYPE_SELF, index=True
    )
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    create_date: Mapped[date] = mapped_column(Date, default=datetime.now)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)

    steps: Mapped[list["OrderStep"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    materials: Mapped[list["OrderMaterial"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    quality_results: Mapped[list["QualityResult"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


STEP_KEYS = ["order", "material", "program", "production", "quality1", "surface", "quality2", "shipping"]
STEP_LABELS = ["接单", "备料", "程序", "投产", "质检1", "表处", "质检2", "出货"]
ORDER_STATUS_LABELS = ["待确认", "备料中", "齐备", "待生产", "质检中", "表处中", "终检中", "待出货"]

# 生产阶段(step 3)有两个显示状态：
#   待生产 = ORDER_STATUS_LABELS[3]，接单后的默认状态
#   生产中 = 有员工的开工单包含该订单，且那张开工单尚未完工
# 「生产中」不落库，由开工单实时判定 —— 否则开工、完工、返工退回三处
# 都得记得同步它，漏一处状态就说谎。
ORDER_STATUS_WAITING_PRODUCTION = "待生产"
ORDER_STATUS_IN_PRODUCTION = "生产中"

# 投产阶段的下标：开工只能选处于该阶段的订单，质检打回也退回到这里
PRODUCTION_STEP = STEP_KEYS.index("production")
# 各质检阶段下标：完工推进到质检1，每阶段判定合格后推进到下一阶段
QUALITY1_STEP = STEP_KEYS.index("quality1")
SURFACE_STEP = STEP_KEYS.index("surface")
QUALITY2_STEP = STEP_KEYS.index("quality2")
SHIPPING_STEP = STEP_KEYS.index("shipping")
# 程序阶段下标：只有这一步能填加工时长
PROGRAM_STEP = STEP_KEYS.index("program")

# 预估工时 × 80% = 加工时长，80% 是加工占整个工时的比例。
# 加工时长单位是分钟、estimated_hours 单位是小时，所以还要除以 60：
#   estimated_hours = machining_minutes ÷ 60 ÷ 0.8 = machining_minutes ÷ 48
MACHINING_RATIO = 0.8
MINUTES_PER_HOUR = 60
_HOURS_DIVISOR = Decimal(str(MINUTES_PER_HOUR)) * Decimal(str(MACHINING_RATIO))
_HOURS_QUANTUM = Decimal("0.01")


def hours_from_machining_minutes(minutes: float) -> float:
    """加工时长（分钟）→ 预估工时（小时），四舍五入保留 2 位小数。

    这里必须用 Decimal + ROUND_HALF_UP，不能用内置 round()：round() 是银行家舍入，
    6 分钟 = 0.125 小时会被舍成 0.12，而前端预览用 JS 的 Math.round 算出 0.13，
    两边对不上，用户看到的预览就和实际存进去的值不一致。
    """
    hours = Decimal(str(minutes)) / _HOURS_DIVISOR
    return float(hours.quantize(_HOURS_QUANTUM, rounding=ROUND_HALF_UP))


# 接单后各类型直接落到的阶段。
# 全外协不在厂内生产，跳过投产直接进质检1；自主与半外协都要在厂内开工。
ORDER_TYPE_TARGET_STEP = {
    ORDER_TYPE_SELF: PRODUCTION_STEP,
    ORDER_TYPE_HALF_OUTSOURCE: PRODUCTION_STEP,
    ORDER_TYPE_FULL_OUTSOURCE: QUALITY1_STEP,
}


def resolve_order_status(order: "Order", in_production_ids: set[str] | None = None) -> str:
    """工单显示状态。

    只有生产阶段(step 3)需要细分：被未完工的开工单包含 → 生产中，否则 → 待生产。
    in_production_ids 由 app.models.production.in_production_order_ids 批量算出，
    调用方一次查询、整批复用，不要逐条查。
    """
    if order.current_step == PRODUCTION_STEP and order.id in (in_production_ids or set()):
        return ORDER_STATUS_IN_PRODUCTION
    return ORDER_STATUS_LABELS[order.current_step]


class OrderStep(Base):
    __tablename__ = "order_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(20))
    step_label: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    operator: Mapped[str | None] = mapped_column(String(50), nullable=True)
    time: Mapped[date | None] = mapped_column(Date, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped["Order"] = relationship(back_populates="steps")


class OrderMaterial(Base):
    __tablename__ = "order_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    stock: Mapped[int] = mapped_column(Integer, default=0)
    required: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ready")

    order: Mapped["Order"] = relationship(back_populates="materials")


class QualityResult(Base):
    """质检结果。stage 区分首检(quality1)与终检(quality2)，同一订单每阶段各一行。"""

    __tablename__ = "quality_results"
    __table_args__ = (
        UniqueConstraint("order_id", "stage", name="uq_quality_result_order_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20), default="quality1")
    pass_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    rate: Mapped[float] = mapped_column(Double, default=0)
    qc_status: Mapped[str] = mapped_column(String(20), default="pass")
    remark: Mapped[str] = mapped_column(String(500), default="")

    order: Mapped["Order"] = relationship(back_populates="quality_results")
