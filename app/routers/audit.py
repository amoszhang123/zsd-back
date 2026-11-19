"""审计日志查询接口。

只读：审计日志不允许从接口修改或删除 —— 能改的日志等于没有日志。
清理靠数据库/运维层面按保留期处理，不开放给页面。
"""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.audit import parse_changes
from app.database import get_db, scoped
from app.models.audit import AuditLog
from app.models.production import Employee
from app.schemas.audit import AuditLogOut, AuditLogPageOut, AuditOptionsOut

router = APIRouter(prefix="/api/audit", tags=["audit"])

MODULE_LABELS = {
    "orders": "接单管理",
    "contracts": "合同/报价",
    "quotes": "报价单",
    "shipments": "出货管理",
    "production": "生产",
    "quality": "质检",
    "costs": "成本支出",
    "cash": "费用收支",
    "revenue": "订单收入",
    "bank": "公账",
    "materials": "物料",
    "machines": "机台",
    "employees": "员工",
    "permissions": "权限",
    "attendance": "考勤",
    "auth": "登录",
}

ACTION_LABELS = {
    "create": "新增",
    "update": "修改",
    "delete": "删除",
    "approve": "批准",
    "reject": "驳回",
    "cancel": "撤销",
    "login": "登录",
    "logout": "退出",
    "import": "导入",
}


def _out(row: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=row.id,
        created_at=row.created_at,
        user_id=row.user_id or "",
        user_name=row.user_name or "",
        user_kind=row.user_kind or "",
        action=row.action or "",
        action_label=ACTION_LABELS.get(row.action or "", row.action or ""),
        module=row.module or "",
        module_label=MODULE_LABELS.get(row.module or "", row.module or ""),
        target_id=row.target_id or "",
        target_label=row.target_label or "",
        summary=row.summary or "",
        changes=parse_changes(row.changes or ""),
        ip=row.ip or "",
    )


@router.get("/logs", response_model=AuditLogPageOut)
def list_logs(
    module: str = Query(default=""),
    user_id: str = Query(default=""),
    action: str = Query(default=""),
    keyword: str = Query(default=""),
    start: str = Query(default=""),
    end: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """分页查审计日志。

    默认只给最近 7 天：日志表会一直长，不设默认范围的话第一次打开就要
    扫全表，越用越慢；要看历史就显式传 start/end。
    """
    q = db.query(AuditLog).filter(scoped(db, AuditLog))

    if module:
        q = q.filter(AuditLog.module == module)
    if user_id:
        q = q.filter(AuditLog.user_id == user_id)
    if action:
        q = q.filter(AuditLog.action == action)

    if start:
        q = q.filter(AuditLog.created_at >= _parse_day(start))
    else:
        q = q.filter(AuditLog.created_at >= datetime.now() - timedelta(days=7))
    if end:
        q = q.filter(AuditLog.created_at < _parse_day(end) + timedelta(days=1))

    kw = keyword.strip()
    if kw:
        like = f"%{kw}%"
        q = q.filter(
            or_(
                AuditLog.summary.like(like),
                AuditLog.target_label.like(like),
                AuditLog.target_id.like(like),
                AuditLog.user_name.like(like),
            )
        )

    total = q.count()
    rows = (
        q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return AuditLogPageOut(
        total=total, page=page, size=size, items=[_out(r) for r in rows]
    )


def _parse_day(value: str) -> datetime:
    """接受 2026-11-19 或 2026-11-19T10:00 两种写法。"""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.combine(date.today(), datetime.min.time())


@router.get("/options", response_model=AuditOptionsOut)
def options(db: Session = Depends(get_db)):
    """筛选项：出现过的模块与操作者。

    人员名从日志表里取而不是员工表：离职员工的操作记录也得能筛出来，
    查员工表会漏掉他们。
    """
    mods = [
        m
        for (m,) in db.query(AuditLog.module).filter(scoped(db, AuditLog)).distinct().all()
        if m
    ]
    users = (
        db.query(AuditLog.user_id, AuditLog.user_name, AuditLog.user_kind)
        .filter(scoped(db, AuditLog), AuditLog.user_id != "")
        .distinct()
        .all()
    )
    # 有工号的补一次现名（改过名的用最新名字），查不到的保留日志里的名字
    emp_names = {e.id: e.name for e in db.query(Employee).all()}
    people = []
    seen = set()
    for uid, name, kind in users:
        if uid in seen:
            continue
        seen.add(uid)
        people.append({
            "user_id": uid,
            "user_name": emp_names.get(uid) or name or uid,
            "user_kind": kind or "",
        })
    people.sort(key=lambda x: x["user_name"])

    return AuditOptionsOut(
        modules=[{"value": m, "label": MODULE_LABELS.get(m, m)} for m in sorted(mods)],
        actions=[
            {"value": k, "label": v}
            for k, v in ACTION_LABELS.items()
            if db.query(func.count(AuditLog.id))
            .filter(scoped(db, AuditLog), AuditLog.action == k)
            .scalar()
        ],
        people=people,
        total=int(
            db.query(func.count(AuditLog.id)).filter(scoped(db, AuditLog)).scalar() or 0
        ),
    )
