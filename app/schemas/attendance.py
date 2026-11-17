from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _clean_hhmm(value: str, field_label: str) -> str:
    """"8:00" / "08:00" / "08：00"（全角冒号）都接受，统一成 "08:00"。

    不校验成 time 类型：这个值会原样回给前端显示，来回转换反而容易在多处
    格式化出 "8:00" 和 "08:00" 两种样子。
    """
    raw = (value or "").strip().replace("：", ":")
    parts = raw.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"{field_label}请填 HH:MM，例如 08:00")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"{field_label}不是有效时间")
    return f"{h:02d}:{m:02d}"


class AttendanceSettingsIn(BaseModel):
    enabled_from: date | None = None
    work_start: str = "08:00"
    work_end: str = "17:00"
    rest_minutes: int = Field(default=60, ge=0, le=240)
    late_grace_minutes: int = Field(default=5, ge=0, le=120)
    # 逗号分隔的 1-7
    workdays: str = "1,2,3,4,5"

    @field_validator("work_start")
    @classmethod
    def v_start(cls, v: str) -> str:
        return _clean_hhmm(v, "上班时间")

    @field_validator("work_end")
    @classmethod
    def v_end(cls, v: str) -> str:
        return _clean_hhmm(v, "下班时间")

    @field_validator("workdays")
    @classmethod
    def v_workdays(cls, v: str) -> str:
        days = sorted({int(x) for x in (v or "").split(",") if x.strip().isdigit() and 1 <= int(x) <= 7})
        if not days:
            raise ValueError("至少要选一个工作日")
        return ",".join(str(d) for d in days)


class AttendanceSettingsOut(BaseModel):
    enabled_from: date | None = None
    work_start: str = "08:00"
    work_end: str = "17:00"
    rest_minutes: int = 60
    late_grace_minutes: int = 5
    workdays: str = "1,2,3,4,5"


class LeaveTypeIn(BaseModel):
    name: str = Field(max_length=20)
    deduct_pay: bool = True
    need_approval: bool = True
    track_balance: bool = False
    color: str = Field(default="#1890ff", max_length=16)
    active: bool = True
    sort: int = 0

    @field_validator("name")
    @classmethod
    def v_name(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("假期名称不能为空")
        return cleaned


class LeaveTypeUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=20)
    deduct_pay: bool | None = None
    need_approval: bool | None = None
    track_balance: bool | None = None
    color: str | None = Field(default=None, max_length=16)
    active: bool | None = None
    sort: int | None = None


class LeaveTypeOut(BaseModel):
    id: int
    name: str
    deduct_pay: bool = True
    need_approval: bool = True
    track_balance: bool = False
    color: str = "#1890ff"
    active: bool = True
    sort: int = 0


class LeaveBalanceOut(BaseModel):
    employee_id: str
    employee_name: str = ""
    type_id: int
    type_name: str = ""
    year: int
    granted: float = 0.0
    used: float = 0.0
    remaining: float = 0.0


class LeaveBalanceUpdate(BaseModel):
    granted: float = Field(ge=0)


class LeaveRequestIn(BaseModel):
    employee_id: str = Field(max_length=20)
    type_id: int
    start_date: date
    end_date: date
    # 单日半天用 0.5 + half=am/pm；其余按整天
    days: float = Field(gt=0)
    half: Literal["", "am", "pm"] = ""
    reason: str = Field(default="", max_length=200)

    @field_validator("reason")
    @classmethod
    def v_reason(cls, v: str) -> str:
        return (v or "").strip()


class LeaveRejectIn(BaseModel):
    reason: str = Field(max_length=200)

    @field_validator("reason")
    @classmethod
    def v_reason(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("驳回必须写理由")
        return cleaned


class LeaveRequestOut(BaseModel):
    id: int
    employee_id: str
    employee_name: str = ""
    type_id: int
    type_name: str = ""
    type_color: str = "#1890ff"
    start_date: date
    end_date: date
    days: float
    half: str = ""
    reason: str = ""
    status: str
    approver_id: str = ""
    approver_name: str = ""
    approved_at: datetime | None = None
    reject_reason: str = ""
    backfilled: bool = False
    created_at: datetime


class AttendanceCellOut(BaseModel):
    """日历上的一格。字段与 attendance_calc.day_status 的返回一一对应。"""

    date: str
    weekday: int
    status: str
    status_label: str
    clock_in: str | None = None
    clock_out: str | None = None
    worked_minutes: int | None = None
    late_minutes: int = 0
    early_minutes: int = 0
    leave_type: str = ""
    leave_days: float = 0
    in_addr: str = ""
    out_addr: str = ""
    source: str = ""
    remark: str = ""
    record_id: int | None = None


class AttendancePersonOut(BaseModel):
    employee_id: str
    name: str = ""
    position: str = ""
    work_start: str = ""
    work_end: str = ""
    days: list[AttendanceCellOut] = []
    summary: dict = {}


class AttendanceCalendarOut(BaseModel):
    month: str
    month_label: str = ""
    workdays: str = "1,2,3,4,5"
    enabled_from: date | None = None
    dates: list[str] = []
    people: list[AttendancePersonOut] = []
    totals: dict = {}


class AttendanceRecordIn(BaseModel):
    """补录/修正某天某人。两个人都有值时表示一次修正，不是追加。"""

    employee_id: str = Field(max_length=20)
    work_date: date
    clock_in: str | None = None  # "08:03"
    clock_out: str | None = None  # "17:10"
    remark: str = Field(default="", max_length=200)

    @field_validator("clock_in", "clock_out")
    @classmethod
    def v_time(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        return _clean_hhmm(v, "打卡时间")

    @field_validator("remark")
    @classmethod
    def v_remark(cls, v: str) -> str:
        return (v or "").strip()


class ClockIn(BaseModel):
    """小程序打卡入参。位置全部可选 —— 拿不到定位不该让人打不了卡。"""

    action: Literal["in", "out"]
    lat: float | None = None
    lng: float | None = None
    addr: str = Field(default="", max_length=200)


class TodayOut(BaseModel):
    """小程序首页/打卡页用：今天的状态 + 班次 + 能不能打。"""

    date: str
    weekday_label: str = ""
    work_start: str = "08:00"
    work_end: str = "17:00"
    clock_in: str | None = None
    clock_out: str | None = None
    status: str = ""
    status_label: str = ""
    leave_type: str = ""
    server_time: str = ""
