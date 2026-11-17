"""考勤状态推导：把「打卡记录 + 班次 + 请假单 + 全厂设置」算成日历上的一种状态。

为什么单独一个模块：web 的矩阵视图、个人的月历、小程序今日卡片、
月度统计四处都要同一套判定。写在 router 里迟早会分叉 —— 出现过一次
「页面上显示正常、统计里算迟到」的口径不一致，排查了半天。

**状态不落库**，每次实时算。存下来就要在补录、改设置、批假、撤假之后
记得重算；忘一处，全表就脏了，而且脏得不容易发现。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

# 状态码。前端按它上色，后端按它统计。
S_BEFORE = "before"          # 启用日之前，不统计
S_PENDING = "pending"        # 还没到那天（未来日期）
S_NOT_YET = "not_yet"        # 就是今天，还没打卡
S_REST = "rest"              # 非工作日，没打卡
S_OFFDAY = "offday"          # 非工作日，但有打卡（值班/加班）
S_ABSENT = "absent"          # 工作日，一次都没打
S_MISSING = "missing"        # 只打了一次（缺上班或下班）
S_LATE = "late"
S_EARLY = "early"
S_LATE_EARLY = "late_early"
S_NORMAL = "normal"
S_LEAVE = "leave"            # 全天请假
S_LEAVE_HALF = "leave_half"  # 半天请假

# 算「实际出勤」的集合
ATTENDED = {S_NORMAL, S_LATE, S_EARLY, S_LATE_EARLY, S_MISSING}
STATUS_LABELS = {
    S_BEFORE: "未启用",
    S_PENDING: "未到",
    S_NOT_YET: "待打卡",
    S_REST: "休息",
    S_OFFDAY: "非工作日上班",
    S_ABSENT: "缺勤",
    S_MISSING: "缺卡",
    S_LATE: "迟到",
    S_EARLY: "早退",
    S_LATE_EARLY: "迟到早退",
    S_NORMAL: "正常",
    S_LEAVE: "请假",
    S_LEAVE_HALF: "半天假",
}

DEFAULT_SETTINGS = {
    "enabled_from": None,
    "work_start": "08:00",
    "work_end": "17:00",
    "rest_minutes": 60,
    "late_grace_minutes": 5,
    "workdays": "1,2,3,4,5",
}


def parse_hhmm(value: str, fallback: str) -> time:
    """"08:00" → time(8,0)。脏数据（空串、手输错的）回落到默认值，
    不让一条错误的设置把整页考勤算崩。"""
    for raw in (value, fallback):
        if not raw:
            continue
        parts = str(raw).strip().split(":")
        if len(parts) == 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return time(h, m)
            except ValueError:
                continue
    return time(8, 0)


def parse_workdays(value: str) -> set[int]:
    """工作日集合（1=周一 … 7=周日）。空或非法一律按周一至周五。"""
    days = set()
    for piece in str(value or "").split(","):
        piece = piece.strip()
        if piece.isdigit() and 1 <= int(piece) <= 7:
            days.add(int(piece))
    return days or {1, 2, 3, 4, 5}


def settings_dict(row) -> dict:
    """设置行 → 普通 dict（没有行时给默认值）。"""
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {
        "enabled_from": row.enabled_from,
        "work_start": row.work_start or DEFAULT_SETTINGS["work_start"],
        "work_end": row.work_end or DEFAULT_SETTINGS["work_end"],
        "rest_minutes": row.rest_minutes if row.rest_minutes is not None else 60,
        "late_grace_minutes": row.late_grace_minutes or 0,
        "workdays": row.workdays or DEFAULT_SETTINGS["workdays"],
    }


def shift_of(employee, settings: dict) -> tuple[time, time, int]:
    """这个人当天的班次：(上班, 下班, 午休分钟)。

    员工档案上的三个字段留空/为 -1 就用全厂设置 —— 只有保洁、门卫这类
    和车间不同点的人才需要单独填。
    """
    start = parse_hhmm(getattr(employee, "work_start", "") or "", settings["work_start"])
    end = parse_hhmm(getattr(employee, "work_end", "") or "", settings["work_end"])
    rest = getattr(employee, "rest_minutes", -1)
    if rest is None or rest < 0:
        rest = settings["rest_minutes"]
    return start, end, int(rest)


def _combine(day: date, t: time) -> datetime:
    return datetime.combine(day, t)


def leave_for(day: date, leaves: list[dict]) -> dict | None:
    """这天有没有请假。leaves 是已按「已批准 + 与本月有交集」筛过的。"""
    for lv in leaves:
        if lv["start_date"] <= day <= lv["end_date"]:
            return lv
    return None


def day_status(
    day: date,
    employee,
    record,
    leave: dict | None,
    settings: dict,
    workdays: set[int],
) -> dict:
    """算出某人某天的状态与展示数据。

    判定优先级（顺序不能换）：
      未启用 → 请假 → 非工作日 → 缺勤/缺卡 → 迟到早退 → 正常
    先判请假，是因为「请了假但忘了撤销打卡」不该算成出勤；
    先判非工作日，是因为周末没打卡是常态，不能算缺勤。
    """
    start_t, end_t, _rest = shift_of(employee, settings)
    grace = timedelta(minutes=settings["late_grace_minutes"])

    out = {
        "date": day.isoformat(),
        "weekday": day.isoweekday(),
        "status": S_NORMAL,
        "status_label": STATUS_LABELS[S_NORMAL],
        "clock_in": None,
        "clock_out": None,
        "worked_minutes": None,
        "late_minutes": 0,
        "early_minutes": 0,
        "leave_type": "",
        "leave_days": 0,
        "in_addr": "",
        "out_addr": "",
        "source": "",
        "remark": "",
        "record_id": None,
    }

    if record is not None:
        out["record_id"] = record.id
        out["source"] = record.source
        out["in_addr"] = record.in_addr or ""
        out["out_addr"] = record.out_addr or ""
        out["remark"] = record.remark or ""
        if record.clock_in:
            out["clock_in"] = record.clock_in.strftime("%H:%M")
        if record.clock_out:
            out["clock_out"] = record.clock_out.strftime("%H:%M")
        if record.clock_in and record.clock_out:
            # 在厂时长：扣掉午休。只扣一次 —— 一天只有一个午休段，
            # 加班到很晚也不会多扣，那部分属于二期的加班核算。
            span = (record.clock_out - record.clock_in).total_seconds() / 60
            out["worked_minutes"] = max(0, int(round(span - _rest)))

    enabled_from = settings.get("enabled_from")
    if enabled_from and day < enabled_from:
        out["status"] = S_BEFORE
        out["status_label"] = STATUS_LABELS[S_BEFORE]
        return out

    if leave:
        out["leave_type"] = leave.get("type_name", "")
        out["leave_days"] = leave.get("days", 1)
        # 半天假只看 half 标记：跨天假在 expand_leaves 里已经被置成 ""，
        # 只有「单日 + 选了半天」才会留下 am/pm。不要再去比 start_date/end_date ——
        # 摊平后的 dict 里根本没有这两个字段（踩过一次 KeyError，整月日历 500）。
        is_half = bool(leave.get("half"))
        out["status"] = S_LEAVE_HALF if is_half else S_LEAVE
        out["status_label"] = STATUS_LABELS[out["status"]]
        return out

    if day.isoweekday() not in workdays:
        has_punch = bool(out["clock_in"] or out["clock_out"])
        out["status"] = S_OFFDAY if has_punch else S_REST
        out["status_label"] = STATUS_LABELS[out["status"]]
        return out

    # 没到的日子不算缺勤。月初看整月时，后面十几天会集体变红 ——
    # 那不是在报异常，是在报「时间还没到」，把统计也一起冲歪了。
    if day > date.today():
        out["status"] = S_PENDING
        out["status_label"] = STATUS_LABELS[S_PENDING]
        return out

    if not out["clock_in"] and not out["clock_out"]:
        # 今天还没打卡不算缺勤：上午十点看页面就给人扣一个「缺勤」，
        # 谁都会以为定性了。缺勤要等这一天过完才成立。
        if day == date.today():
            out["status"] = S_NOT_YET
            out["status_label"] = STATUS_LABELS[S_NOT_YET]
            return out
        out["status"] = S_ABSENT
        out["status_label"] = STATUS_LABELS[S_ABSENT]
        return out

    # 缺卡：只打了一次。不判迟到早退 —— 少一次打卡时算出来的分钟数没意义
    if not out["clock_in"] or not out["clock_out"]:
        out["status"] = S_MISSING
        out["status_label"] = STATUS_LABELS[S_MISSING]
        return out

    in_dt = record.clock_in
    out_dt = record.clock_out
    dead_line = _combine(day, start_t) + grace
    if in_dt > dead_line:
        out["late_minutes"] = int((in_dt - _combine(day, start_t)).total_seconds() // 60)
    leave_line = _combine(day, end_t) - grace
    if out_dt < leave_line:
        out["early_minutes"] = int((_combine(day, end_t) - out_dt).total_seconds() // 60)

    if out["late_minutes"] and out["early_minutes"]:
        out["status"] = S_LATE_EARLY
    elif out["late_minutes"]:
        out["status"] = S_LATE
    elif out["early_minutes"]:
        out["status"] = S_EARLY
    else:
        out["status"] = S_NORMAL
    out["status_label"] = STATUS_LABELS[out["status"]]
    return out


def month_days(month: str) -> tuple[date, date]:
    """'2026-11' → (11-01, 11-30)。"""
    try:
        y, m = (int(x) for x in month.split("-"))
        start = date(y, m, 1)
    except (ValueError, AttributeError):
        raise ValueError("月份格式应为 YYYY-MM")
    end = (start.replace(year=y + 1, month=1) if m == 12 else start.replace(month=m + 1)) - timedelta(days=1)
    return start, end


def iter_days(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def summarize(rows: list[dict], workdays: set[int], enabled_from: date | None) -> dict:
    """把一整个月的 day_status 结果汇总成几个数字。

    口径：
      * 应出勤 = 这段时间里的工作日天数（启用日前的不算）
      * 实际出勤 = 正常/迟到/早退/缺卡（缺卡算来了，只是少一次打卡）
      * 请假天数 = 各请假记录的 days 之和（半天算 0.5）
      * 缺勤 = 应出勤 − 实际出勤 − 请假占用的工作日（不硬减，直接数状态）
    """
    expected = 0
    attended = 0
    absent = 0
    missing = 0
    late = 0
    early = 0
    leave_days = 0.0
    worked_minutes = 0

    for row in rows:
        status = row["status"]
        if status in (S_BEFORE, S_PENDING, S_NOT_YET):
            continue
        day = date.fromisoformat(row["date"])
        if day.isoweekday() in workdays:
            expected += 1
        if status in ATTENDED:
            attended += 1
            worked_minutes += row["worked_minutes"] or 0
        if status == S_ABSENT:
            absent += 1
        if status == S_MISSING:
            missing += 1
        if status in (S_LATE, S_LATE_EARLY):
            late += 1
        if status in (S_EARLY, S_LATE_EARLY):
            early += 1
        if status in (S_LEAVE, S_LEAVE_HALF):
            leave_days += row["leave_days"] or 0

    return {
        "expected_days": expected,
        "attended_days": attended,
        "absent_days": absent,
        "missing_days": missing,
        "late_count": late,
        "early_count": early,
        "leave_days": round(leave_days, 1),
        "worked_hours": round(worked_minutes / 60, 1),
    }
