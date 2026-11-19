"""考勤接口：设置、假期类型、余额、日历、补录、请假与审批。

一期不含工资。所有「迟到几分钟、缺勤几天」都是实时算的，
数据库里只存原始事实（打卡时间、请假单），见 app/attendance_calc.py 的说明。
"""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import attendance_calc as calc
from app import audit
from app.database import get_db, scoped
from app.models.attendance import (
    LEAVE_APPROVED,
    LEAVE_CANCELLED,
    LEAVE_PENDING,
    LEAVE_REJECTED,
    SOURCE_MANUAL,
    AttendanceRecord,
    AttendanceSettings,
    LeaveBalance,
    LeaveRequest,
    LeaveType,
)
from app.models.production import Employee
from app.schemas.attendance import (
    AttendanceCalendarOut,
    AttendancePersonOut,
    AttendanceRecordIn,
    AttendanceSettingsIn,
    AttendanceSettingsOut,
    LeaveBalanceOut,
    LeaveBalanceUpdate,
    LeaveRejectIn,
    LeaveRequestIn,
    LeaveRequestOut,
    LeaveTypeIn,
    LeaveTypeOut,
    LeaveTypeUpdate,
)

router = APIRouter(prefix="/api/attendance", tags=["attendance"])

WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 不参与考勤的工种：老板/管理员不打卡，把他算进来会让每天的「缺勤」都多一个人，
# 统计看着像全厂没人上班。真出现「行政岗也要免打卡」的需求时，再给员工表加
# 一个 attendance_required 字段，别在这里堆工种名单。
EXCLUDED_POSITIONS = {"管理员"}


# ---------------- 设置 ----------------

def get_settings_row(db: Session) -> AttendanceSettings | None:
    return db.query(AttendanceSettings).order_by(AttendanceSettings.id).first()


def settings_or_default(db: Session) -> dict:
    return calc.settings_dict(get_settings_row(db))


@router.get("/settings", response_model=AttendanceSettingsOut)
def read_settings(db: Session = Depends(get_db)):
    return AttendanceSettingsOut(**settings_or_default(db))


@router.put("/settings", response_model=AttendanceSettingsOut)
def save_settings(req: AttendanceSettingsIn, db: Session = Depends(get_db)):
    if req.work_start >= req.work_end:
        raise HTTPException(400, "下班时间要晚于上班时间")
    row = get_settings_row(db)
    payload = req.model_dump()
    if row is None:
        row = AttendanceSettings(**payload)
        db.add(row)
    else:
        for field, value in payload.items():
            setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return AttendanceSettingsOut(**calc.settings_dict(row))


# ---------------- 假期类型 ----------------

def _type_out(t: LeaveType) -> LeaveTypeOut:
    return LeaveTypeOut(
        id=t.id,
        name=t.name,
        deduct_pay=bool(t.deduct_pay),
        need_approval=bool(t.need_approval),
        track_balance=bool(t.track_balance),
        color=t.color or "#1890ff",
        active=bool(t.active),
        sort=t.sort or 0,
    )


@router.get("/types", response_model=list[LeaveTypeOut])
def list_types(db: Session = Depends(get_db)):
    rows = db.query(LeaveType).order_by(LeaveType.sort, LeaveType.id).all()
    return [_type_out(t) for t in rows]


@router.post("/types", response_model=LeaveTypeOut)
def create_type(req: LeaveTypeIn, db: Session = Depends(get_db)):
    exists = db.query(LeaveType).filter(LeaveType.name == req.name).first()
    if exists:
        raise HTTPException(400, f"已经有叫「{req.name}」的假期类型了")
    row = LeaveType(**req.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return _type_out(row)


@router.patch("/types/{type_id}", response_model=LeaveTypeOut)
def update_type(type_id: int, req: LeaveTypeUpdate, db: Session = Depends(get_db)):
    row = db.query(LeaveType).filter(LeaveType.id == type_id).first()
    if not row:
        raise HTTPException(404, "假期类型不存在")
    changes = req.model_dump(exclude_unset=True)
    name = changes.get("name")
    if name is not None:
        name = name.strip()
        if not name:
            raise HTTPException(400, "假期名称不能为空")
        dup = (
            db.query(LeaveType)
            .filter(LeaveType.name == name, LeaveType.id != type_id)
            .first()
        )
        if dup:
            raise HTTPException(400, f"已经有叫「{name}」的假期类型了")
        changes["name"] = name
    for field, value in changes.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return _type_out(row)


@router.delete("/types/{type_id}")
def delete_type(type_id: int, db: Session = Depends(get_db)):
    """有请假单引用过的类型不删，只停用 —— 删了会让历史单据变成孤儿。"""
    row = db.query(LeaveType).filter(LeaveType.id == type_id).first()
    if not row:
        raise HTTPException(404, "假期类型不存在")
    used = (
        db.query(func.count(LeaveRequest.id))
        .filter(scoped(db, LeaveRequest), LeaveRequest.type_id == type_id)
        .scalar()
        or 0
    )
    if used:
        row.active = False
        db.commit()
        return {"deleted": False, "deactivated": True, "used": used}
    db.delete(row)
    db.commit()
    return {"deleted": True, "deactivated": False, "used": 0}


# ---------------- 请假余额 ----------------

def _balance_rows(db: Session, year: int) -> list[LeaveBalanceOut]:
    types = {t.id: t for t in db.query(LeaveType).all()}
    emps = {e.id: e for e in db.query(Employee).all()}
    rows = (
        db.query(LeaveBalance)
        .filter(scoped(db, LeaveBalance), LeaveBalance.year == year)
        .all()
    )
    out = []
    for b in rows:
        t = types.get(b.type_id)
        if t is None or not t.track_balance:
            continue
        emp = emps.get(b.employee_id)
        granted = float(b.granted or 0)
        used = float(b.used or 0)
        out.append(
            LeaveBalanceOut(
                employee_id=b.employee_id,
                employee_name=emp.name if emp else "",
                type_id=b.type_id,
                type_name=t.name,
                year=year,
                granted=granted,
                used=used,
                remaining=round(granted - used, 1),
            )
        )
    out.sort(key=lambda x: (x.type_name, x.employee_id))
    return out


@router.get("/balances", response_model=list[LeaveBalanceOut])
def list_balances(
    year: int = Query(default=0), db: Session = Depends(get_db)
):
    return _balance_rows(db, year or datetime.now().year)


@router.put("/balances/{employee_id}/{type_id}", response_model=LeaveBalanceOut)
def set_balance(
    employee_id: str,
    type_id: int,
    req: LeaveBalanceUpdate,
    year: int = Query(default=0),
    db: Session = Depends(get_db),
):
    """调整某人某年的额度。已用天数不动 —— 改额度不该动已经请掉的假。"""
    year = year or datetime.now().year
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(404, f"员工不存在: {employee_id}")
    ltype = db.query(LeaveType).filter(LeaveType.id == type_id).first()
    if not ltype:
        raise HTTPException(404, "假期类型不存在")
    if not ltype.track_balance:
        raise HTTPException(400, f"「{ltype.name}」不按余额管理，不需要设额度")

    row = (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
            LeaveBalance.type_id == type_id,
        )
        .first()
    )
    if row is None:
        row = LeaveBalance(
            employee_id=employee_id, year=year, type_id=type_id, granted=req.granted, used=0
        )
        db.add(row)
    else:
        row.granted = req.granted
    db.commit()
    db.refresh(row)
    return LeaveBalanceOut(
        employee_id=employee_id,
        employee_name=emp.name,
        type_id=type_id,
        type_name=ltype.name,
        year=year,
        granted=float(row.granted or 0),
        used=float(row.used or 0),
        remaining=round(float(row.granted or 0) - float(row.used or 0), 1),
    )


# ---------------- 请假单 ----------------

def _leave_type_map(db: Session) -> dict[int, LeaveType]:
    return {t.id: t for t in db.query(LeaveType).all()}


def _employee_map(db: Session) -> dict[str, Employee]:
    return {e.id: e for e in db.query(Employee).all()}


def _leave_out(
    lv: LeaveRequest,
    types: dict[int, LeaveType],
    emps: dict[str, Employee],
) -> LeaveRequestOut:
    t = types.get(lv.type_id)
    emp = emps.get(lv.employee_id)
    approver = emps.get(lv.approver_id) if lv.approver_id else None
    return LeaveRequestOut(
        id=lv.id,
        employee_id=lv.employee_id,
        employee_name=emp.name if emp else "",
        type_id=lv.type_id,
        type_name=t.name if t else "",
        type_color=(t.color if t else "") or "#1890ff",
        start_date=lv.start_date,
        end_date=lv.end_date,
        days=float(lv.days or 0),
        half=lv.half or "",
        reason=lv.reason or "",
        status=lv.status,
        approver_id=lv.approver_id or "",
        approver_name=approver.name if approver else "",
        approved_at=lv.approved_at,
        reject_reason=lv.reject_reason or "",
        backfilled=bool(lv.backfilled),
        created_at=lv.created_at,
    )


def approved_leaves_between(
    db: Session, start: date, end: date, employee_id: str | None = None
) -> list[LeaveRequest]:
    """与 [start, end] 有交集的已批准请假。"""
    q = db.query(LeaveRequest).filter(
        scoped(db, LeaveRequest),
        LeaveRequest.status == LEAVE_APPROVED,
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
    )
    if employee_id:
        q = q.filter(LeaveRequest.employee_id == employee_id)
    return q.all()


def expand_leaves(db: Session, start: date, end: date) -> dict[tuple[str, date], dict]:
    """把请假单摊成「某人某天」的字典。

    跨天假每一天都按整天记（提交时已校验跨天不能是半天）：
    这样统计时逐日累加就得到总天数，不会因为一张 3 天的单子被算成 3×3 天。
    """
    types = _leave_type_map(db)
    out: dict[tuple[str, date], dict] = {}
    for lv in approved_leaves_between(db, start, end):
        t = types.get(lv.type_id)
        name = t.name if t else ""
        if lv.start_date == lv.end_date:
            out[(lv.employee_id, lv.start_date)] = {
                "type_name": name,
                "days": float(lv.days or 0),
                "half": lv.half or "",
                "request_id": lv.id,
            }
            continue
        for day in calc.iter_days(lv.start_date, lv.end_date):
            if start <= day <= end:
                out[(lv.employee_id, day)] = {
                    "type_name": name,
                    "days": 1.0,
                    "half": "",
                    "request_id": lv.id,
                }
    return out


@router.get("/leaves", response_model=list[LeaveRequestOut])
def list_leaves(
    status: str = Query(default=""),
    month: str = Query(default=""),
    employee_id: str = Query(default=""),
    db: Session = Depends(get_db),
):
    q = db.query(LeaveRequest).filter(scoped(db, LeaveRequest))
    if status:
        q = q.filter(LeaveRequest.status == status)
    if employee_id:
        q = q.filter(LeaveRequest.employee_id == employee_id)
    if month:
        try:
            start, end = calc.month_days(month)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        q = q.filter(LeaveRequest.start_date <= end, LeaveRequest.end_date >= start)
    rows = q.order_by(LeaveRequest.created_at.desc()).all()
    types, emps = _leave_type_map(db), _employee_map(db)
    return [_leave_out(lv, types, emps) for lv in rows]


def _validate_leave_days(req: LeaveRequestIn) -> None:
    """天数与起止日期必须自洽，否则统计和余额都会算错。"""
    if req.end_date < req.start_date:
        raise HTTPException(400, "结束日期不能早于开始日期")
    span = (req.end_date - req.start_date).days + 1
    if span == 1:
        if req.half:
            if req.days != 0.5:
                raise HTTPException(400, "半天假的天数应当是 0.5 天")
        elif req.days != 1:
            raise HTTPException(400, "单日请假的天数应当是 1 天（或选半天）")
    else:
        if req.half:
            raise HTTPException(400, "跨天请假不能只请半天，请把起止日期改成同一天")
        if req.days != span:
            raise HTTPException(400, f"从 {req.start_date} 到 {req.end_date} 共 {span} 天，天数应为 {span}")


def _balance_of(db: Session, employee_id: str, type_id: int, year: int) -> LeaveBalance | None:
    return (
        db.query(LeaveBalance)
        .filter(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
            LeaveBalance.type_id == type_id,
        )
        .first()
    )


@router.post("/leaves", response_model=LeaveRequestOut)
def create_leave(
    req: LeaveRequestIn,
    backfilled: bool = Query(default=False),
    created_by: str = Query(default="web"),
    db: Session = Depends(get_db),
):
    """管理员在 web 端代提请假（小程序端走 /api/mp/attendance/leaves）。"""
    _validate_leave_days(req)
    emp = db.query(Employee).filter(Employee.id == req.employee_id).first()
    if not emp:
        raise HTTPException(404, f"员工不存在: {req.employee_id}")
    ltype = db.query(LeaveType).filter(LeaveType.id == req.type_id).first()
    if not ltype or not ltype.active:
        raise HTTPException(400, "假期类型不存在或已停用")

    if ltype.track_balance:
        year = req.start_date.year
        bal = _balance_of(db, req.employee_id, req.type_id, year)
        remaining = (float(bal.granted) - float(bal.used)) if bal else 0.0
        if req.days > remaining + 1e-6:
            raise HTTPException(
                400,
                f"「{ltype.name}」剩余 {remaining:g} 天，不够请 {req.days:g} 天",
            )

    # 不要求审批的类型提交即生效（比如厂里自己记的调休）
    auto = not ltype.need_approval
    row = LeaveRequest(
        employee_id=req.employee_id,
        type_id=req.type_id,
        start_date=req.start_date,
        end_date=req.end_date,
        days=req.days,
        half=req.half,
        reason=req.reason,
        status=LEAVE_APPROVED if auto else LEAVE_PENDING,
        approver_id="自动" if auto else "",
        approved_at=datetime.now() if auto else None,
        created_by=created_by,
        backfilled=backfilled,
    )
    db.add(row)
    if auto and ltype.track_balance:
        _apply_balance(db, req.employee_id, req.type_id, req.start_date.year, req.days)
    db.commit()
    db.refresh(row)
    types, emps = _leave_type_map(db), _employee_map(db)
    return _leave_out(row, types, emps)


def _apply_balance(db: Session, employee_id: str, type_id: int, year: int, delta: float) -> None:
    """余额增减。正数=用掉，负数=退回。没有余额行时补一条空的。"""
    bal = _balance_of(db, employee_id, type_id, year)
    if bal is None:
        bal = LeaveBalance(employee_id=employee_id, year=year, type_id=type_id, granted=0, used=0)
        db.add(bal)
        db.flush()
    bal.used = max(0.0, float(bal.used or 0) + delta)


def _get_leave_or_404(db: Session, leave_id: int) -> LeaveRequest:
    row = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not row:
        raise HTTPException(404, "请假单不存在")
    return row


@router.post("/leaves/{leave_id}/approve", response_model=LeaveRequestOut)
def approve_leave(
    leave_id: int,
    approver: str = Query(default="web"),
    db: Session = Depends(get_db),
):
    row = _get_leave_or_404(db, leave_id)
    if row.status != LEAVE_PENDING:
        raise HTTPException(400, "这张请假单已经处理过了")
    ltype = db.query(LeaveType).filter(LeaveType.id == row.type_id).first()
    if ltype and ltype.track_balance:
        # 提交时不校验余额（可能同时有多张待批），批准这一刻才是真正占用
        year = row.start_date.year
        bal = _balance_of(db, row.employee_id, row.type_id, year)
        remaining = (float(bal.granted or 0) - float(bal.used or 0)) if bal else 0.0
        if float(row.days) > remaining + 1e-6:
            raise HTTPException(
                400,
                f"「{ltype.name}」只剩 {remaining:g} 天，批不了这 {float(row.days):g} 天。"
                "请先调整额度或驳回重提",
            )
        _apply_balance(db, row.employee_id, row.type_id, year, float(row.days))
    row.status = LEAVE_APPROVED
    row.approver_id = approver
    row.approved_at = datetime.now()
    emp = db.query(Employee).filter(Employee.id == row.employee_id).first()
    audit.record(
        db, action="approve", module="attendance",
        target_id=str(row.id),
        target_label=f"请假 {emp.name if emp else row.employee_id} {row.start_date}",
        summary=f"批准请假：{emp.name if emp else row.employee_id} "
                f"{row.start_date}{'~' + str(row.end_date) if row.end_date != row.start_date else ''} "
                f"{row.days:g} 天（{ltype.name if ltype else ''}）",
    )
    db.commit()
    db.refresh(row)
    types, emps = _leave_type_map(db), _employee_map(db)
    return _leave_out(row, types, emps)


@router.post("/leaves/{leave_id}/reject", response_model=LeaveRequestOut)
def reject_leave(
    leave_id: int,
    req: LeaveRejectIn,
    approver: str = Query(default="web"),
    db: Session = Depends(get_db),
):
    row = _get_leave_or_404(db, leave_id)
    if row.status != LEAVE_PENDING:
        raise HTTPException(400, "这张请假单已经处理过了")
    row.status = LEAVE_REJECTED
    row.reject_reason = req.reason
    row.approver_id = approver
    row.approved_at = datetime.now()
    emp = db.query(Employee).filter(Employee.id == row.employee_id).first()
    audit.record(
        db, action="reject", module="attendance",
        target_id=str(row.id),
        target_label=f"请假 {emp.name if emp else row.employee_id} {row.start_date}",
        summary=f"驳回请假：{emp.name if emp else row.employee_id} {row.start_date}，理由：{req.reason}",
    )
    db.commit()
    db.refresh(row)
    types, emps = _leave_type_map(db), _employee_map(db)
    return _leave_out(row, types, emps)


@router.post("/leaves/{leave_id}/cancel", response_model=LeaveRequestOut)
def cancel_leave(
    leave_id: int,
    operator: str = Query(default="web"),
    db: Session = Depends(get_db),
):
    """撤销已批准的假。

    只允许撤销「今天及以后」的：撤掉上个月的假会改掉那个月的出勤和扣款，
    而那个月可能已经结算过了。
    """
    row = _get_leave_or_404(db, leave_id)
    if row.status != LEAVE_APPROVED:
        raise HTTPException(400, "只有已批准的请假可以撤销")
    if row.start_date < date.today():
        raise HTTPException(
            400, "这张请假已经开始（或已过去），不能撤销。如确需修改，请联系管理员处理历史数据"
        )
    ltype = db.query(LeaveType).filter(LeaveType.id == row.type_id).first()
    if ltype and ltype.track_balance:
        _apply_balance(db, row.employee_id, row.type_id, row.start_date.year, -float(row.days))
    row.status = LEAVE_CANCELLED
    row.approver_id = operator
    row.approved_at = datetime.now()
    emp = db.query(Employee).filter(Employee.id == row.employee_id).first()
    audit.record(
        db, action="cancel", module="attendance",
        target_id=str(row.id),
        target_label=f"请假 {emp.name if emp else row.employee_id} {row.start_date}",
        summary=f"撤销请假：{emp.name if emp else row.employee_id} {row.start_date} {row.days:g} 天",
    )
    db.commit()
    db.refresh(row)
    types, emps = _leave_type_map(db), _employee_map(db)
    return _leave_out(row, types, emps)


# ---------------- 打卡记录（补录） ----------------

def _parse_day_time(day: date, hhmm: str) -> datetime:
    t = calc.parse_hhmm(hhmm, "00:00")
    return datetime.combine(day, t)


@router.post("/records", response_model=AttendancePersonOut)
def upsert_record(req: AttendanceRecordIn, db: Session = Depends(get_db)):
    """补录或修正某天某人的打卡（管理员用）。

    只写原始时间，状态由查询时实时算 —— 所以补录完不用"重算"任何东西。
    """
    emp = db.query(Employee).filter(Employee.id == req.employee_id).first()
    if not emp:
        raise HTTPException(404, f"员工不存在: {req.employee_id}")

    if req.clock_in and req.clock_out:
        in_dt = _parse_day_time(req.work_date, req.clock_in)
        out_dt = _parse_day_time(req.work_date, req.clock_out)
        if out_dt <= in_dt:
            # 跨夜班一期不支持：固定白班不会有，真要上夜班得先定班次模型
            raise HTTPException(400, "下班时间要晚于上班时间（跨夜班暂不支持）")

    row = (
        db.query(AttendanceRecord)
        .filter(
            AttendanceRecord.employee_id == req.employee_id,
            AttendanceRecord.work_date == req.work_date,
        )
        .first()
    )
    if row is None:
        row = AttendanceRecord(
            employee_id=req.employee_id,
            work_date=req.work_date,
            source=SOURCE_MANUAL,
            operator_id="web",
        )
        db.add(row)
    row.clock_in = _parse_day_time(req.work_date, req.clock_in) if req.clock_in else None
    row.clock_out = _parse_day_time(req.work_date, req.clock_out) if req.clock_out else None
    row.source = SOURCE_MANUAL
    row.remark = req.remark
    # 补录会直接改考勤结论（迟到变正常、缺勤变出勤），必须留痕
    audit.record(
        db, action="update", module="attendance",
        target_id=str(row.id),
        target_label=f"考勤 {emp.name} {req.work_date}",
        summary=f"补录打卡：{emp.name} {req.work_date} "
                f"{req.clock_in or '—'} ~ {req.clock_out or '—'}"
                + (f"（{req.remark}）" if req.remark else ""),
    )
    db.commit()

    # 返回这个人这个月的日历，前端直接替换掉该行，不用整页刷新
    return _person_month(db, emp, req.work_date.strftime("%Y-%m"))


@router.delete("/records/{record_id}")
def delete_record(record_id: int, db: Session = Depends(get_db)):
    row = db.query(AttendanceRecord).filter(AttendanceRecord.id == record_id).first()
    if not row:
        raise HTTPException(404, "打卡记录不存在")
    db.delete(row)
    db.commit()
    return {"deleted": record_id}


# ---------------- 日历 ----------------

def _person_month(db: Session, emp: Employee, month: str) -> AttendancePersonOut:
    settings = settings_or_default(db)
    workdays = calc.parse_workdays(settings["workdays"])
    start, end = calc.month_days(month)
    recs = {
        r.work_date: r
        for r in db.query(AttendanceRecord)
        .filter(
            scoped(db, AttendanceRecord),
            AttendanceRecord.employee_id == emp.id,
            AttendanceRecord.work_date >= start,
            AttendanceRecord.work_date <= end,
        )
        .all()
    }
    leaves = expand_leaves(db, start, end)
    days = []
    for day in calc.iter_days(start, end):
        days.append(
            calc.day_status(
                day,
                emp,
                recs.get(day),
                leaves.get((emp.id, day)),
                settings,
                workdays,
            )
        )
    start_t, end_t, _ = calc.shift_of(emp, settings)
    return AttendancePersonOut(
        employee_id=emp.id,
        name=emp.name,
        position=emp.position or "",
        work_start=start_t.strftime("%H:%M"),
        work_end=end_t.strftime("%H:%M"),
        days=days,
        summary=calc.summarize(days, workdays, settings.get("enabled_from")),
    )


@router.get("/calendar", response_model=AttendanceCalendarOut)
def calendar(
    month: str = Query(default=""),
    employee_id: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """一个月内所有人（或指定某人）的考勤日历。

    一次把全月数据都返回：20 人 × 31 天 = 620 格，JSON 约 150KB，
    比前端按人按天发请求简单得多，翻月份也只是一次请求。
    """
    month = month or datetime.now().strftime("%Y-%m")
    try:
        start, end = calc.month_days(month)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    settings = settings_or_default(db)
    workdays = calc.parse_workdays(settings["workdays"])
    dates = [d.isoformat() for d in calc.iter_days(start, end)]

    q = db.query(Employee).filter(Employee.status == "在职")
    if employee_id:
        q = q.filter(Employee.id == employee_id)
    else:
        # 指定了某人时不过滤工种：主动查管理员本人的考勤是合法诉求
        q = q.filter(~Employee.position.in_(EXCLUDED_POSITIONS))
    employees = q.order_by(Employee.id).all()

    leaves = expand_leaves(db, start, end)

    records: dict[tuple[str, date], AttendanceRecord] = {}
    for r in (
        db.query(AttendanceRecord)
        .filter(
            scoped(db, AttendanceRecord),
            AttendanceRecord.work_date >= start,
            AttendanceRecord.work_date <= end,
        )
        .all()
    ):
        records[(r.employee_id, r.work_date)] = r

    people: list[AttendancePersonOut] = []
    totals = {
        "expected_days": 0,
        "attended_days": 0,
        "absent_days": 0,
        "missing_days": 0,
        "late_count": 0,
        "early_count": 0,
        "leave_days": 0.0,
        "worked_hours": 0.0,
    }
    for emp in employees:
        start_t, end_t, _rest = calc.shift_of(emp, settings)
        days = []
        for day in calc.iter_days(start, end):
            days.append(
                calc.day_status(
                    day,
                    emp,
                    records.get((emp.id, day)),
                    leaves.get((emp.id, day)),
                    settings,
                    workdays,
                )
            )
        summary = calc.summarize(days, workdays, settings.get("enabled_from"))
        for key in totals:
            totals[key] = round(totals[key] + summary[key], 1)
        people.append(
            AttendancePersonOut(
                employee_id=emp.id,
                name=emp.name,
                position=emp.position or "",
                work_start=start_t.strftime("%H:%M"),
                work_end=end_t.strftime("%H:%M"),
                days=days,
                summary=summary,
            )
        )

    # 未处理的请假数一并返回，页签上要显示待办角标
    pending = (
        db.query(func.count(LeaveRequest.id))
        .filter(scoped(db, LeaveRequest), LeaveRequest.status == LEAVE_PENDING)
        .scalar()
        or 0
    )
    totals["pending_leaves"] = pending

    return AttendanceCalendarOut(
        month=month,
        month_label=f"{month[:4]} 年 {int(month[5:])} 月",
        workdays=settings["workdays"],
        enabled_from=settings.get("enabled_from"),
        dates=dates,
        people=people,
        totals=totals,
    )
