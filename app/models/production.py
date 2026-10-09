from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.database import Base


# 员工属性（用工形式）：直属 / 外包。新建时默认直属，
# 外包需要显式选择 —— 存量员工也都按直属落库，见
# migrations/20261009_employees_employee_type.sql。
EMPLOYEE_TYPE_DIRECT = "直属员工"
EMPLOYEE_TYPE_OUTSOURCED = "外包员工"
EMPLOYEE_TYPES = (EMPLOYEE_TYPE_DIRECT, EMPLOYEE_TYPE_OUTSOURCED)

# 岗位。目前只有「编程」「质检」两个岗位参与代码判定（小程序「我的订单」按岗位
# 决定列表口径、以及能不能编辑程序时间 / 做质检判定）；操作工只是 web 端下拉里
# 的选项，不做逻辑分支。
POSITION_PROGRAMMER = "编程"
POSITION_QC = "质检"


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    gender: Mapped[str] = mapped_column(String(4), default="男")
    id_card: Mapped[str] = mapped_column(String(18), default="")
    phone: Mapped[str] = mapped_column(String(20), default="", index=True)
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="在职")
    position: Mapped[str] = mapped_column(String(20), default="操作工")
    employee_type: Mapped[str] = mapped_column(String(10), default=EMPLOYEE_TYPE_DIRECT)


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), default="")


class WorkOrder(Base):
    """开工单（派工单）：可含多名负责员工、多个订单。

    同一名员工名下同时只能有一张**未完工**的开工单（必须完工结单才能开下一张），
    但一天开几张不限 —— 原先的「一人一天一张」已放开，
    见 migrations/20261008_drop_employee_work_date_unique.sql。
    """

    __tablename__ = "work_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    machine_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("machines.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(10), default="开工", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    machine: Mapped["Machine | None"] = relationship()
    employees: Mapped[list["WorkOrderEmployee"]] = relationship(
        back_populates="work_order", cascade="all, delete-orphan"
    )
    items: Mapped[list["WorkOrderItem"]] = relationship(
        back_populates="work_order", cascade="all, delete-orphan"
    )


class WorkOrderEmployee(Base):
    __tablename__ = "work_order_employees"
    __table_args__ = (
        UniqueConstraint("work_order_id", "employee_id", name="uq_work_order_employee"),
        # work_date 冗余自父表，只用于按日期查询。
        # 这里原先还有 UNIQUE(employee_id, work_date) 硬保证「一人一天一张」，
        # 该规则已放开（改为「名下没有未完工的单就能再开」，由应用层保证），
        # 约束也随之删除，见 migrations/20261008_drop_employee_work_date_unique.sql。
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_order_id: Mapped[int] = mapped_column(Integer, ForeignKey("work_orders.id"))
    # 普通索引，外键 employee_id → employees.id 要靠它支撑。
    # 原先这个活是 UNIQUE(employee_id, work_date) 顶着的，那条约束已删除
    # （一人一天可开多张），所以必须显式补一个，否则 DROP INDEX 会报 1553。
    employee_id: Mapped[str] = mapped_column(
        String(10), ForeignKey("employees.id"), index=True
    )
    work_date: Mapped[date] = mapped_column(Date)

    work_order: Mapped["WorkOrder"] = relationship(back_populates="employees")
    employee: Mapped["Employee"] = relationship()


class WorkOrderItem(Base):
    __tablename__ = "work_order_items"
    __table_args__ = (UniqueConstraint("work_order_id", "order_id", name="uq_work_order_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_order_id: Mapped[int] = mapped_column(Integer, ForeignKey("work_orders.id"))
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    completed_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)

    work_order: Mapped["WorkOrder"] = relationship(back_populates="items")
    order: Mapped["Order"] = relationship()


class OrderContribution(Base):
    """订单参与度：一个订单由多名员工分多个开工单接力完成时，记录各自占比。

    由把订单做到全量完成的那名员工在完工时统一填写，合计必须为 100%。
    只在全量完成那一刻写一次，因此 (order_id, employee_id) 唯一。
    """

    __tablename__ = "order_contributions"
    __table_args__ = (
        UniqueConstraint("order_id", "employee_id", name="uq_order_contribution"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    employee_id: Mapped[str] = mapped_column(String(20), ForeignKey("employees.id"), index=True)
    percent: Mapped[int] = mapped_column(Integer, default=0)
    # 谁在哪张开工单上填的，便于事后追溯
    work_order_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("work_orders.id"), nullable=True
    )
    filled_by: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    order: Mapped["Order"] = relationship()
    employee: Mapped["Employee"] = relationship()


class OrderProgrammer(Base):
    """订单 ↔ 编程人。

    一张订单可能被多名编程员工先后接手（改刀路、补工序、返工重编），所以用关联表
    而不是在 orders 上放一个 programmer_id —— 那样换人接手就会把前一个人抹掉。

    谁保存过程序时间（orders.machining_minutes），就在这里记一行，
    小程序「我的订单」里「我参与的已编程订单」靠它查。
    """

    __tablename__ = "order_programmers"
    __table_args__ = (
        UniqueConstraint("order_id", "employee_id", name="uq_order_programmer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.id"), index=True)
    employee_id: Mapped[str] = mapped_column(String(20), ForeignKey("employees.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    order: Mapped["Order"] = relationship()
    employee: Mapped["Employee"] = relationship()


from app.models.order import Order  # noqa: E402

# 开工单状态只有这两个取值（见 app/routers/production.py 的 start_work / complete_work）
WORK_ORDER_STATUS_OPEN = "开工"
WORK_ORDER_STATUS_DONE = "完工"


def in_production_order_ids(db: Session, order_ids: list[str] | None = None) -> set[str]:
    """处于「生产中」的工单 id 集合：被某张**未完工**的开工单包含。

    只认未完工的开工单，是因为返工/维修会把工单退回生产阶段(step 3)，
    而它早先那张开工单已经完工、开工明细还留着 —— 这种工单是在等重新开工，
    应显示「待生产」而不是「生产中」。

    order_ids 传 None 表示不限范围（整库）；传空列表直接返回空集，避免 IN () 。
    """
    query = (
        db.query(WorkOrderItem.order_id)
        .join(WorkOrder, WorkOrder.id == WorkOrderItem.work_order_id)
        .filter(WorkOrder.status != WORK_ORDER_STATUS_DONE)
    )
    if order_ids is not None:
        if not order_ids:
            return set()
        query = query.filter(WorkOrderItem.order_id.in_(order_ids))
    return {row[0] for row in query.all()}
