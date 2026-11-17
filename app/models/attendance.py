"""考勤：打卡记录、请假单、假期类型与余额、全厂班次设置。

一期只做「出勤事实 + 请假审批 + 出勤日历」，不碰工资。
扣款（事假折薪）是二期的事 —— 它需要月薪字段和一套扣款口径，
在考勤数据还没攒够一个月的时候就上线，等于拿错误数据算钱。

几个刻意的取舍：

* **不在记录里存 status**。迟到/早退/缺卡都是「打卡时间 + 班次 + 设置」算出来的，
  存下来就会和打卡时间打架：改一条补录要记得重算状态，忘一次全表就脏了。
  所以状态一律由 app/attendance_calc.py 实时推导。
* **请假不写进打卡记录**。批准一张请假单只是在查询时把那天涂成「假」，
  删掉请假单那天立刻恢复正常判定，不需要回头清理打卡数据。
* **打卡时间用服务器时间**。手机本地时间可以随便改，员工把时间调到 08:00
  再打卡，用客户端时间的系统就会认。客户端时间只用来显示。
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Double,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantScoped

# 请假单状态
LEAVE_PENDING = "pending"
LEAVE_APPROVED = "approved"
LEAVE_REJECTED = "rejected"
LEAVE_CANCELLED = "cancelled"

# 打卡来源
SOURCE_MP = "mp"
SOURCE_MANUAL = "manual"


class AttendanceSettings(TenantScoped, Base):
    """全厂考勤设置。恒只有一行（id=1），没有就按默认值用。

    做成表而不是配置文件：老板要在页面上改上下班时间和启用日期，
    改配置文件得重启容器，而且 .env 里加一堆考勤参数更容易配错。
    """

    __tablename__ = "attendance_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 启用日期：这天之前的日期不判缺勤、不参与统计。
    # 上线当月已经过去的日子，真实打卡在纸质表里，系统凭空判「旷工 17 天」会吓死人。
    enabled_from: Mapped[date | None] = mapped_column(Date, nullable=True)

    work_start: Mapped[str] = mapped_column(String(5), default="08:00")
    work_end: Mapped[str] = mapped_column(String(5), default="17:00")
    # 午休分钟数：只用于算工时（一天 9 小时减去 60 分钟 = 8 小时），不影响打卡
    rest_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # 迟到宽限：机床旁洗手换鞋都要时间，卡到分秒只会天天吵架
    late_grace_minutes: Mapped[int] = mapped_column(Integer, default=5)
    # 工作日，逗号分隔的 1-7（周一到周日）。默认周一到周五；
    # 周六是否上班写在这里，而不是在代码里假设「周末=休息」
    workdays: Mapped[str] = mapped_column(String(20), default="1,2,3,4,5")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class LeaveType(TenantScoped, Base):
    """假期类型。只配三个开关，不做「扣款公式可编程」那种东西。

    三个开关能覆盖厂里所有假：婚假（不扣、要审、不管余额）、年假（不扣、要审、管余额）、
    事假（扣、要审、不管余额）。再复杂就该进财务系统了。
    """

    __tablename__ = "leave_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(20))
    # 是否扣薪：二期算扣款时读它。一期只影响页面上的标色和提示。
    deduct_pay: Mapped[bool] = mapped_column(Boolean, default=True)
    # 是否需审批：关掉的话提交即生效（比如「调休」在有些厂里自己记就行）
    need_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    # 是否管余额：年假/调休要，事假病假不要
    track_balance: Mapped[bool] = mapped_column(Boolean, default=False)
    # 日历上的颜色，前端直接用
    color: Mapped[str] = mapped_column(String(16), default="#1890ff")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class LeaveBalance(TenantScoped, Base):
    """假期余额：员工 × 年份 × 类型。

    只在 track_balance 的类型上生效。批准请假时 used 增加，撤销时减少 ——
    used 是「已批准占用的天数」，不是「已用完的天数」，被驳回的不算。
    """

    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint("employee_id", "year", "type_id", name="uq_leave_balance"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(String(20), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    type_id: Mapped[int] = mapped_column(Integer, index=True)
    # 全年额度（天）。年假常见「入职满一年 5 天」，手工填 + 每年调整
    granted: Mapped[float] = mapped_column(Double, default=0.0)
    used: Mapped[float] = mapped_column(Double, default=0.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class LeaveRequest(TenantScoped, Base):
    """一张请假单。

    半天用 days=0.5 + half 标记（am/pm），跨天假只记整天 ——
    「周三上午请两小时看牙」在厂里是打个招呼的事，进系统反而添乱。
    """

    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(String(20), index=True)
    type_id: Mapped[int] = mapped_column(Integer, index=True)

    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    # 请假天数（已按半天折算），0.5 的倍数
    days: Mapped[float] = mapped_column(Double, default=1.0)
    # "" = 整天；"am" / "pm" = 单日半天
    half: Mapped[str] = mapped_column(String(2), default="")

    reason: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(10), default=LEAVE_PENDING, index=True)

    approver_id: Mapped[str] = mapped_column(String(20), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(String(200), default="")

    # 谁提交的：员工自己提的是自己工号；管理员代提记管理员工号 + backfilled=True
    created_by: Mapped[str] = mapped_column(String(20), default="")
    # 补录标记：事后补请假要在统计里能看出来
    backfilled: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class AttendanceRecord(TenantScoped, Base):
    """一名员工某一天的打卡（可能只有上班、也可能两次都有）。

    一人一天一条，靠唯一约束挡住重复打卡：手机上连点两下「上班打卡」，
    第二次会因为这条约束失败，前端提示「今天已经打过了」。
    """

    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_attendance_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(String(20), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)

    clock_in: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clock_out: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 位置留底，不拦截。GPS 在铁皮厂房里漂移几百米是常事，
    # 硬拦会让「人在车间却打不上卡」，比考勤漏洞更招怨。
    in_lat: Mapped[float | None] = mapped_column(Double, nullable=True)
    in_lng: Mapped[float | None] = mapped_column(Double, nullable=True)
    in_addr: Mapped[str] = mapped_column(String(200), default="")
    out_lat: Mapped[float | None] = mapped_column(Double, nullable=True)
    out_lng: Mapped[float | None] = mapped_column(Double, nullable=True)
    out_addr: Mapped[str] = mapped_column(String(200), default="")

    # mp = 员工自己打的；manual = 管理员补录/修正
    source: Mapped[str] = mapped_column(String(10), default=SOURCE_MP)
    # 补录人（source=manual 时有值），出问题能追到人
    operator_id: Mapped[str] = mapped_column(String(20), default="")
    remark: Mapped[str] = mapped_column(String(200), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
