"""小程序「物料领取」接口。

和 web 端 /api/materials 的关系：
- web 端那套是**仓管视角**（选员工、连扫多料、代还代废），接口本身没鉴权；
- 这里是小程序**员工视角**，每个接口都从 token 反查员工，只能看和动自己的账。

领料 / 归还的业务校验不在这里重写，直接复用 routers/materials.py 的
create_issue / return_issue —— 库存恒等式、单号生成、流水 before/after 快照
只允许有一份实现，小程序再抄一遍迟早抄错。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.inventory import (
    ISSUE_OPEN,
    MOVE_ISSUE,
    MOVE_RETURN,
    MOVE_SCRAP,
    MOVE_TYPE_LABELS,
    IssueItem,
    IssueOrder,
    Material,
    MaterialMovement,
)
from app.models.production import Employee
from app.routers.materials import (
    ZERO,
    _d,
    _f,
    _unsettled_for,
    create_issue,
    return_issue,
)
from app.routers.mp import _current_employee, _get_mp_user
from app.schemas.inventory import IssueCreate, IssueItemIn, ReturnItemIn, ReturnRequest

router = APIRouter(prefix="/api/mp/materials", tags=["mp-materials"])

# 记录页要展示的三类流水。入库/撤销入库是仓管的事，员工看不到也不关心。
RECORD_TYPES = (MOVE_ISSUE, MOVE_RETURN, MOVE_SCRAP)

POSITION_ADMIN = "管理员"


def _require_admin(emp: Employee) -> None:
    """全厂库存只给管理员看。前端只决定入口显不显示，这里必须是硬拦。"""
    if emp.position != POSITION_ADMIN:
        raise HTTPException(403, "只有管理员可以查看全厂库存")


# ------------------------------------------------------------------ 出参

class MpMaterialBrief(BaseModel):
    id: int
    code: str
    name: str
    spec: str = ""
    unit: str = "个"
    category: str = ""
    location: str = ""
    stock_qty: float = 0
    must_return: bool = False
    active: bool = True


class MpMaterialScanOut(BaseModel):
    """扫一条物料码的结果。扫错了不报错，返回 found=false 让前端提示重扫。"""

    found: bool
    material: MpMaterialBrief | None = None
    # 能不能领 + 为什么不能领。block_reason 非空就置灰确认按钮。
    can_issue: bool = False
    block_reason: str = ""
    # 这个人名下这一样料还欠多少（必须归还时才可能 > 0）
    open_qty: float = 0
    open_order_nos: list[str] = []


class MpIssueRequest(BaseModel):
    material_id: int
    qty: float = Field(gt=0)
    remark: str = Field(default="", max_length=500)


class MpIssueResultOut(BaseModel):
    order_no: str
    material_name: str
    unit: str
    qty: float
    stock_qty: float
    issued_at: datetime


class MpOpenItemOut(BaseModel):
    """未结清的一行。归还按钮带的就是 item_id —— 归还落在行上，不落在物料上。"""

    item_id: int
    issue_order_id: int
    order_no: str
    material_id: int
    material_code: str
    material_name: str
    unit: str
    qty: float
    returned_qty: float
    scrapped_qty: float
    open_qty: float
    must_return: bool = False
    issued_at: datetime


class MpReturnRequest(BaseModel):
    qty: float = Field(gt=0)


class MpReturnResultOut(BaseModel):
    order_no: str
    material_name: str
    unit: str
    returned_qty: float
    left_qty: float
    order_settled: bool


class MpRecordOut(BaseModel):
    id: int
    mtype: str
    mtype_label: str
    material_id: int
    material_code: str = ""
    material_name: str = ""
    unit: str = ""
    qty: float = 0
    ref_no: str = ""
    remark: str = ""
    created_at: datetime


class MpStockRowOut(BaseModel):
    id: int
    code: str
    name: str
    spec: str = ""
    location: str = ""
    unit: str = "个"
    stock_qty: float = 0
    pending_qty: float = 0
    warn_threshold: float = 0
    warn: bool = False
    shortage_ratio: float = 0
    active: bool = True
    must_return: bool = False


class MpStockOut(BaseModel):
    rows: list[MpStockRowOut] = []
    material_count: int = 0
    warn_count: int = 0
    zero_count: int = 0


# ------------------------------------------------------------------ 工具

def _brief(m: Material) -> MpMaterialBrief:
    return MpMaterialBrief(
        id=m.id, code=m.code, name=m.name, spec=m.spec or "", unit=m.unit or "个",
        category=m.category or "", location=m.location or "",
        stock_qty=_f(m.stock_qty), must_return=bool(m.must_return), active=bool(m.active),
    )


def _normalize_code(raw: str) -> str:
    """物料码就是 materials.code。扫码结果里带空格/换行、或大小写不一致都很常见，
    直接 upper + 去空白后精确匹配 —— 不做模糊匹配，模糊匹配在扫码场景等于猜。"""
    return (raw or "").strip().upper()


# ------------------------------------------------------------------ 扫码取料

@router.get("/scan", response_model=MpMaterialScanOut)
def scan_material(
    code: str = Query(min_length=1),
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """扫物料条码 → 物料信息 + 能不能领。

    这里的 can_issue 只是**提前告知**，真正的拦截在 create_issue 里。
    两处都判一遍是有意的：前端要立刻把按钮置灰，后端要挡住直接调接口的请求。
    """
    emp = _current_employee(user, db)
    material = db.query(Material).filter(Material.code == _normalize_code(code)).first()
    if not material:
        return MpMaterialScanOut(found=False)

    open_qty, order_nos = ZERO, []
    if material.must_return:
        unsettled = _unsettled_for(db, emp.id, [material.id]).get(material.id)
        if unsettled:
            open_qty, order_nos = unsettled

    block = ""
    if not material.active:
        block = f"「{material.name}」已停用，不能再领"
    elif _d(material.stock_qty) <= ZERO:
        block = f"「{material.name}」库存为 0，等采购入库后再领"
    elif open_qty > ZERO:
        block = (
            f"你还欠 {_f(open_qty)}{material.unit}「{material.name}」"
            f"（{'、'.join(order_nos[:2])}{'…' if len(order_nos) > 2 else ''}），"
            "先归还再领"
        )

    return MpMaterialScanOut(
        found=True, material=_brief(material),
        can_issue=not block, block_reason=block,
        open_qty=_f(open_qty), open_order_nos=order_nos,
    )


# ------------------------------------------------------------------ 领料

@router.post("/issues", response_model=MpIssueResultOut)
def create_my_issue(
    req: MpIssueRequest,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """一人一料一张单。走 create_issue，库存校验 / 必须归还拦截 / 单号生成全在那儿。"""
    emp = _current_employee(user, db)
    order = create_issue(
        IssueCreate(
            employee_id=emp.id,
            items=[IssueItemIn(material_id=req.material_id, qty=req.qty)],
            remark=req.remark,
        ),
        db,
    )
    item = order.items[0] if order.items else None
    material = db.get(Material, req.material_id)
    return MpIssueResultOut(
        order_no=order.order_no,
        material_name=item.material_name if item else (material.name if material else ""),
        unit=item.unit if item else (material.unit if material else ""),
        qty=req.qty,
        stock_qty=_f(material.stock_qty) if material else 0,
        issued_at=order.issued_at,
    )


# ------------------------------------------------------------------ 未结清

@router.get("/open-items", response_model=list[MpOpenItemOut])
def my_open_items(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """我名下所有「领了还没还也没废」的行，最新在前。

    按行列、不按物料聚合：归还接口的入参是 item_id，聚合成「共欠 3 把」之后
    就没法确定冲哪一行了。
    """
    emp = _current_employee(user, db)
    rows = (
        db.query(IssueItem, IssueOrder, Material)
        .join(IssueOrder, IssueOrder.id == IssueItem.issue_order_id)
        .join(Material, Material.id == IssueItem.material_id)
        .filter(
            IssueOrder.employee_id == emp.id,
            IssueOrder.status == ISSUE_OPEN,
        )
        .order_by(IssueOrder.issued_at.desc(), IssueItem.id.desc())
        .all()
    )
    out: list[MpOpenItemOut] = []
    for it, order, material in rows:
        open_qty = _d(it.qty) - _d(it.returned_qty) - _d(it.scrapped_qty)
        if open_qty <= ZERO:
            continue
        out.append(
            MpOpenItemOut(
                item_id=it.id, issue_order_id=order.id, order_no=order.order_no,
                material_id=it.material_id, material_code=it.material_code,
                material_name=it.material_name, unit=it.unit,
                qty=_f(it.qty), returned_qty=_f(it.returned_qty),
                scrapped_qty=_f(it.scrapped_qty), open_qty=_f(open_qty),
                must_return=bool(material.must_return), issued_at=order.issued_at,
            )
        )
    return out


# ------------------------------------------------------------------ 归还

def _owned_open_item(db: Session, item_id: int, employee_id: str) -> tuple[IssueItem, IssueOrder]:
    item = db.get(IssueItem, item_id)
    if not item:
        raise HTTPException(404, "领用记录不存在")
    order = db.get(IssueOrder, item.issue_order_id)
    if not order:
        raise HTTPException(404, "领用单不存在")
    # 只能还自己的：token 里的手机号反查出来的员工，必须就是这张单的人
    if order.employee_id != employee_id:
        raise HTTPException(403, "这条领用记录不在你名下，不能归还")
    if order.status != ISSUE_OPEN:
        raise HTTPException(400, "这张领用单已经全部结清了")
    return item, order


@router.post("/issue-items/{item_id}/return", response_model=MpReturnResultOut)
def return_my_item(
    item_id: int,
    req: MpReturnRequest,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """归还。小程序只有归还、没有报废 —— 报废是仓管在 web 端做的事。

    仍然走 return_issue：它负责流水、行上的已还数量、以及「全部结清才转已归还」
    的状态机。这里只补一条「这行是不是你的」。
    """
    emp = _current_employee(user, db)
    item, order = _owned_open_item(db, item_id, emp.id)
    open_qty = _d(item.qty) - _d(item.returned_qty) - _d(item.scrapped_qty)
    if _d(req.qty) > open_qty:
        raise HTTPException(
            400,
            f"「{item.material_name}」未结清只有 {_f(open_qty)}{item.unit}，"
            f"本次要还 {_f(req.qty)}{item.unit}",
        )

    updated = return_issue(
        order.id,
        ReturnRequest(
            items=[ReturnItemIn(item_id=item_id, return_qty=req.qty, scrap_qty=0)],
            reason="",
        ),
        db,
    )
    left = ZERO
    for line in updated.items:
        if line.id == item_id:
            left = _d(line.open_qty)
            break
    return MpReturnResultOut(
        order_no=updated.order_no,
        material_name=item.material_name,
        unit=item.unit,
        returned_qty=req.qty,
        left_qty=_f(left),
        order_settled=updated.status != ISSUE_OPEN,
    )


# ------------------------------------------------------------------ 记录

@router.get("/records", response_model=list[MpRecordOut])
def my_records(
    limit: int = Query(default=30, ge=1, le=100),
    before_id: int | None = Query(default=None),
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """我的领料 / 归还 / 报废流水，倒序，游标分页（before_id = 上一页最后一条的 id）。

    用 id 游标而不是 offset：流水只追加，翻页途中新插一条会让 offset 分页
    重复或漏行。
    """
    emp = _current_employee(user, db)
    q = (
        db.query(MaterialMovement, Material)
        .outerjoin(Material, Material.id == MaterialMovement.material_id)
        .filter(
            MaterialMovement.employee_id == emp.id,
            MaterialMovement.mtype.in_(RECORD_TYPES),
        )
    )
    if before_id:
        q = q.filter(MaterialMovement.id < before_id)
    rows = q.order_by(MaterialMovement.id.desc()).limit(limit).all()
    return [
        MpRecordOut(
            id=mv.id, mtype=mv.mtype,
            mtype_label=MOVE_TYPE_LABELS.get(mv.mtype, mv.mtype),
            material_id=mv.material_id,
            # 流水只存了物料名快照（建档时抄的），编码和单位要现查主数据
            material_code=(m.code if m else ""),
            material_name=mv.material_name or (m.name if m else ""),
            unit=(m.unit if m else ""),
            qty=_f(mv.qty), ref_no=mv.ref_no or "", remark=mv.remark or "",
            created_at=mv.created_at,
        )
        for mv, m in rows
    ]


# ------------------------------------------------------------------ 全厂库存

@router.get("/stock", response_model=MpStockOut)
def material_stock(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """全厂库存，管理员只读。排序与 web 端 list_materials 完全一致：
    告急优先 → 缺口比例降序 → 编码，这样两端翻出来的顺序是同一套。"""
    emp = _current_employee(user, db)
    _require_admin(emp)

    materials = db.query(Material).all()
    # 「领用中」的合计：列表里显示「其中 3 把在外面」，仓管才知道库存为什么低
    pending: dict[int, float] = {}
    for mid, total in (
        db.query(
            IssueItem.material_id,
            func.sum(IssueItem.qty - IssueItem.returned_qty - IssueItem.scrapped_qty),
        )
        .join(IssueOrder, IssueOrder.id == IssueItem.issue_order_id)
        .filter(IssueOrder.status == ISSUE_OPEN)
        .group_by(IssueItem.material_id)
        .all()
    ):
        if total and float(total) > 0:
            pending[mid] = float(total)

    rows: list[MpStockRowOut] = []
    for m in materials:
        threshold, stock = _d(m.warn_threshold), _d(m.stock_qty)
        warn = bool(m.active) and threshold > ZERO and stock < threshold
        rows.append(
            MpStockRowOut(
                id=m.id, code=m.code, name=m.name, spec=m.spec or "",
                location=m.location or "", unit=m.unit or "个",
                stock_qty=_f(stock), pending_qty=round(pending.get(m.id, 0.0), 2),
                warn_threshold=_f(threshold), warn=warn,
                shortage_ratio=round(float((threshold - stock) / threshold), 4) if warn else 0,
                active=bool(m.active), must_return=bool(m.must_return),
            )
        )
    rows.sort(key=lambda r: (0 if r.warn else 1, -r.shortage_ratio, r.code))
    return MpStockOut(
        rows=rows,
        material_count=len(rows),
        warn_count=sum(1 for r in rows if r.warn),
        zero_count=sum(1 for r in rows if r.active and r.stock_qty <= 0),
    )


@router.get("/alerts")
def material_alerts(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """首页入口上的告警角标。只回数字，不拉全量 —— 工作台每次 onShow 都会调它。"""
    emp = _current_employee(user, db)
    if emp.position != POSITION_ADMIN:
        return {"warn_count": 0, "is_admin": False}
    materials = db.query(Material).filter(Material.active.is_(True)).all()
    warn_count = sum(
        1
        for m in materials
        if _d(m.warn_threshold) > ZERO and _d(m.stock_qty) < _d(m.warn_threshold)
    )
    return {"warn_count": warn_count, "is_admin": True}
