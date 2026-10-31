"""物料管理接口：台账、入库（成本纳入）、领用、归还/报废、流水、标签。

记账的唯一不变量：

    入库累计 = 存量 + 报废 + 领用中

所有改动库存的动作都必须走 _move()，它同时改 materials 的三列并追加一条流水，
before/after 快照一起落库。绕过 _move 直接改列，恒等式就会在某次「先还一半
再报废一半」之后悄悄破掉，而且事后查不出来。

接口鉴权沿用项目现状：模块可见性由 permissions 页控制，接口本身不拦。
"""
import base64
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.material_label import MaterialLabelData, label_png, render_label_sheet
from app.models.cost import CostRecord
from app.models.inventory import (
    ISSUE_CLOSED,
    ISSUE_OPEN,
    ISSUE_STATUS_LABELS,
    MOVE_IN,
    MOVE_ISSUE,
    MOVE_RETURN,
    MOVE_REVOKE,
    MOVE_SCRAP,
    MOVE_TYPE_LABELS,
    SCRAP_FROM_ISSUE,
    SCRAP_FROM_STOCK,
    SCRAP_SOURCE_LABELS,
    IssueItem,
    IssueOrder,
    Material,
    MaterialMovement,
    MaterialScrapItem,
    MaterialScrapOrder,
)
from app.models.production import Employee
from app.schemas.inventory import (
    IssueCreate,
    IssueItemOut,
    IssueOrderOut,
    LabelPagesOut,
    LabelRequest,
    MaterialCreate,
    MaterialListOut,
    MaterialOut,
    MaterialSummary,
    MaterialUpdate,
    MovementOut,
    ReturnRequest,
    ScrapItemOut,
    ScrapOrderOut,
    StockInRequest,
    StockScrapRequest,
)

router = APIRouter(prefix="/api/materials", tags=["materials"])

CENT = Decimal("0.01")
ZERO = Decimal("0")


def _d(value) -> Decimal:
    """统一量化到两位小数。DECIMAL 列读出来是 Decimal，成本记录是 float，
    直接相加会抛 TypeError，所以入口一律过这一层。"""
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(CENT, ROUND_HALF_UP)


def _f(value) -> float:
    return float(value or 0)


# ---------------------------------------------------------------- 编号生成

def _next_material_code(db: Session) -> str:
    """WL000001 递增。code 定长零填充，所以字符串 max() 就是数值 max()。"""
    last = (
        db.query(func.max(Material.code)).filter(Material.code.like("WL%")).scalar()
    )
    seq = 1
    if last and re.fullmatch(r"WL\d{6}", last):
        seq = int(last[2:]) + 1
    return f"WL{seq:06d}"


def _next_daily_no(db: Session, prefix: str, column, day: date) -> str:
    """单据号：前缀 + 日期 + 4 位当日流水，如 LL202610290001。"""
    head = f"{prefix}{day:%Y%m%d}"
    last = db.query(func.max(column)).filter(column.like(f"{head}%")).scalar()
    seq = 1
    if last and last[len(head):].isdigit():
        seq = int(last[len(head):]) + 1
    return f"{head}{seq:04d}"


# ---------------------------------------------------------------- 记账

def _move(
    db: Session,
    material: Material,
    mtype: str,
    qty: Decimal,
    *,
    d_stock: Decimal = ZERO,
    d_issued: Decimal = ZERO,
    d_scrapped: Decimal = ZERO,
    ref_type: str = "",
    ref_id: int | None = None,
    ref_no: str = "",
    employee: Employee | None = None,
    remark: str = "",
) -> MaterialMovement:
    """改三列 + 追加流水。所有库存变动的唯一入口。"""
    qty = _d(qty)
    s0, i0, x0 = _d(material.stock_qty), _d(material.issued_qty), _d(material.scrapped_qty)

    material.stock_qty = s0 + _d(d_stock)
    material.issued_qty = i0 + _d(d_issued)
    material.scrapped_qty = x0 + _d(d_scrapped)
    material.updated_at = datetime.now()

    mv = MaterialMovement(
        material_id=material.id,
        mtype=mtype,
        qty=qty,
        stock_before=s0, stock_after=material.stock_qty,
        issued_before=i0, issued_after=material.issued_qty,
        scrapped_before=x0, scrapped_after=material.scrapped_qty,
        ref_type=ref_type, ref_id=ref_id, ref_no=ref_no,
        employee_id=employee.id if employee else None,
        employee_name=employee.name if employee else "",
        material_name=material.name,
        remark=remark,
        created_at=datetime.now(),
    )
    db.add(mv)
    return mv


def _pending_map(db: Session) -> dict[int, Decimal]:
    """每种物料「领出去了但还没还也没废」的数量。列表里的 pending_qty 用它。"""
    rows = (
        db.query(
            IssueItem.material_id,
            func.sum(IssueItem.qty - IssueItem.returned_qty - IssueItem.scrapped_qty),
        )
        .join(IssueOrder, IssueOrder.id == IssueItem.issue_order_id)
        .filter(IssueOrder.status == ISSUE_OPEN)
        .group_by(IssueItem.material_id)
        .all()
    )
    # SUM 在 SQL 里是 DECIMAL，MySQL 会给 Decimal，统一过 _d 免得和 float 混用
    return {mid: _d(total) for mid, total in rows if _d(total) > ZERO}


def _material_out(m: Material, pending: dict[int, Decimal]) -> MaterialOut:
    threshold = _d(m.warn_threshold)
    stock = _d(m.stock_qty)
    warn = bool(m.active) and threshold > ZERO and stock < threshold
    ratio = 0.0
    if warn:
        ratio = round(float((threshold - stock) / threshold), 4)
    return MaterialOut(
        id=m.id, code=m.code, name=m.name, spec=m.spec, unit=m.unit,
        category=m.category, location=m.location,
        warn_threshold=_f(threshold), active=bool(m.active),
        stock_qty=_f(stock), issued_qty=_f(m.issued_qty), scrapped_qty=_f(m.scrapped_qty),
        total_in=_f(stock + _d(m.issued_qty) + _d(m.scrapped_qty)),
        pending_qty=_f(pending.get(m.id, ZERO)),
        warn=warn, shortage_ratio=ratio,
        remark=m.remark, created_at=m.created_at, updated_at=m.updated_at,
    )


def _get_material(db: Session, material_id: int) -> Material:
    m = db.get(Material, material_id)
    if not m:
        raise HTTPException(404, f"物料不存在: #{material_id}")
    return m


def _get_employee(db: Session, employee_id: str) -> Employee:
    emp = db.get(Employee, employee_id)
    if not emp:
        raise HTTPException(404, f"员工不存在: {employee_id}")
    return emp


# ---------------------------------------------------------------- 物料主数据

@router.get("", response_model=MaterialListOut)
def list_materials(db: Session = Depends(get_db)):
    """库存列表。告警物料置顶，按缺口比例降序。

    不分页也不加服务端筛选：和成本、员工等页面一致，筛选在前端做，
    否则会出现「服务端先滤一遍、前端再滤一遍」的双层过滤。
    """
    materials = db.query(Material).all()
    pending = _pending_map(db)

    rows = [_material_out(m, pending) for m in materials]
    rows.sort(key=lambda r: (0 if r.warn else 1, -r.shortage_ratio, r.code))

    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    summary = MaterialSummary(
        material_count=len(rows),
        active_count=sum(1 for r in rows if r.active),
        warn_count=sum(1 for r in rows if r.warn),
        zero_count=sum(1 for r in rows if r.active and r.stock_qty <= 0),
        open_issue_count=db.query(IssueOrder).filter(IssueOrder.status == ISSUE_OPEN).count(),
        month_issue_count=db.query(IssueOrder).filter(IssueOrder.issued_at >= month_start).count(),
        month_scrap_count=db.query(MaterialScrapOrder)
        .filter(MaterialScrapOrder.created_at >= month_start).count(),
    )
    return MaterialListOut(rows=rows, summary=summary)


@router.post("", response_model=MaterialOut)
def create_material(req: MaterialCreate, db: Session = Depends(get_db)):
    """建档，库存固定 0。贴标签和入库解耦：先把码贴上料架，等采购到货再纳库存。"""
    material = Material(
        code=_next_material_code(db),
        name=req.name, spec=req.spec, unit=req.unit, category=req.category,
        location=req.location, warn_threshold=_d(req.warn_threshold),
        remark=req.remark, active=True,
        stock_qty=ZERO, issued_qty=ZERO, scrapped_qty=ZERO,
    )
    for attempt in range(3):
        try:
            db.add(material)
            db.commit()
            break
        except IntegrityError:
            db.rollback()
            material.code = _next_material_code(db)
    else:
        raise HTTPException(409, "物料编码生成冲突，请重试")
    db.refresh(material)
    return _material_out(material, {})


@router.patch("/{material_id}", response_model=MaterialOut)
def update_material(
    material_id: int, req: MaterialUpdate, db: Session = Depends(get_db)
):
    material = _get_material(db, material_id)
    for field, value in req.model_dump(exclude_unset=True).items():
        if field == "warn_threshold" and value is not None:
            setattr(material, field, _d(value))
        else:
            setattr(material, field, value)
    db.commit()
    db.refresh(material)
    return _material_out(material, _pending_map(db))


# ---------------------------------------------------------------- 入库

@router.post("/stock-in", response_model=MaterialOut)
def stock_in(req: StockInRequest, db: Session = Depends(get_db)):
    """把一条成本记录纳入库存。一条成本记录只能纳一次，撤销后可重纳。"""
    record = db.get(CostRecord, req.cost_record_id)
    if not record:
        raise HTTPException(404, f"成本记录不存在: #{req.cost_record_id}")
    if record.material_id:
        raise HTTPException(400, "这笔成本已经纳入过库存，请先撤销入库再重新纳入")

    if req.new_material:
        material = Material(
            code=_next_material_code(db),
            name=req.new_material.name, spec=req.new_material.spec,
            unit=req.new_material.unit, category=req.new_material.category,
            location=req.new_material.location,
            warn_threshold=_d(req.new_material.warn_threshold),
            remark=req.new_material.remark, active=True,
            stock_qty=ZERO, issued_qty=ZERO, scrapped_qty=ZERO,
        )
        db.add(material)
        db.flush()
    else:
        material = _get_material(db, req.material_id)

    qty = _d(req.quantity if req.quantity is not None else record.quantity)
    if qty <= ZERO:
        raise HTTPException(400, "入库数量必须大于 0")

    record.material_id = material.id
    record.stocked_qty = qty
    record.stocked_at = datetime.now()

    _move(
        db, material, MOVE_IN, qty, d_stock=qty,
        ref_type="cost_record", ref_id=record.id, ref_no=f"成本 #{record.id}",
        remark=req.remark or f"采购纳入：{record.category} {record.spec}".strip(),
    )
    db.commit()
    db.refresh(material)
    return _material_out(material, _pending_map(db))


@router.delete("/stock-in/{cost_record_id}", response_model=MaterialOut)
def revoke_stock_in(cost_record_id: int, db: Session = Depends(get_db)):
    """撤销入库。只在该物料当前存量够扣时才允许 —— 料已经被领走还硬撤，库存会变负。"""
    record = db.get(CostRecord, cost_record_id)
    if not record:
        raise HTTPException(404, f"成本记录不存在: #{cost_record_id}")
    if not record.material_id:
        raise HTTPException(400, "这笔成本还没纳入库存")

    material = _get_material(db, record.material_id)
    qty = _d(record.stocked_qty)
    if _d(material.stock_qty) < qty:
        raise HTTPException(
            400,
            f"无法撤销：{material.name} 当前存量 {_f(material.stock_qty)}，"
            f"少于这笔入库的 {_f(qty)}（料已被领用或报废）",
        )

    _move(
        db, material, MOVE_REVOKE, qty, d_stock=-qty,
        ref_type="cost_record", ref_id=record.id, ref_no=f"成本 #{record.id}",
        remark="撤销入库",
    )
    record.material_id = None
    record.stocked_qty = None
    record.stocked_at = None
    db.commit()
    db.refresh(material)
    return _material_out(material, _pending_map(db))


# ---------------------------------------------------------------- 领用

def _issue_items_out(db: Session, orders: list[IssueOrder]) -> dict[int, list[IssueItemOut]]:
    if not orders:
        return {}
    ids = [o.id for o in orders]
    items = (
        db.query(IssueItem)
        .filter(IssueItem.issue_order_id.in_(ids))
        .order_by(IssueItem.id)
        .all()
    )
    out: dict[int, list[IssueItemOut]] = {oid: [] for oid in ids}
    for it in items:
        open_qty = _d(it.qty) - _d(it.returned_qty) - _d(it.scrapped_qty)
        out[it.issue_order_id].append(
            IssueItemOut(
                id=it.id, material_id=it.material_id,
                material_code=it.material_code, material_name=it.material_name,
                unit=it.unit, qty=_f(it.qty), returned_qty=_f(it.returned_qty),
                scrapped_qty=_f(it.scrapped_qty), open_qty=_f(open_qty),
            )
        )
    return out


def _issue_out(o: IssueOrder, items: list[IssueItemOut]) -> IssueOrderOut:
    return IssueOrderOut(
        id=o.id, order_no=o.order_no, employee_id=o.employee_id,
        employee_name=o.employee_name, status=o.status,
        status_label=ISSUE_STATUS_LABELS.get(o.status, o.status),
        items_summary=o.items_summary, remark=o.remark,
        issued_at=o.issued_at, returned_at=o.returned_at, items=items,
    )


@router.post("/issues", response_model=IssueOrderOut)
def create_issue(req: IssueCreate, db: Session = Depends(get_db)):
    """仓管扫员工码 + 连扫物料码后提交。

    同一物料扫多次在前端就累加（扫两次 = 数量 2），这里再兜一次合并，
    防止接口被直接调用时同一物料出现两行 —— 两行的库存校验会各自通过，
    合起来却超卖。
    """
    employee = _get_employee(db, req.employee_id)

    merged: dict[int, Decimal] = {}
    for line in req.items:
        merged[line.material_id] = merged.get(line.material_id, ZERO) + _d(line.qty)

    materials: dict[int, Material] = {}
    for material_id, qty in merged.items():
        if qty <= ZERO:
            raise HTTPException(400, "领用数量必须大于 0")
        m = _get_material(db, material_id)
        if not m.active:
            raise HTTPException(400, f"物料「{m.name}」已停用，不能领用")
        if _d(m.stock_qty) < qty:
            raise HTTPException(
                400,
                f"物料「{m.name}」库存不足：现有 {_f(m.stock_qty)}{m.unit}，"
                f"本次要领 {_f(qty)}{m.unit}",
            )
        materials[material_id] = m

    names = [materials[mid].name for mid in merged]
    summary = "、".join(names)
    if len(summary) > 280:
        summary = summary[:277] + "…"

    today = date.today()
    order = None
    for attempt in range(3):
        order = IssueOrder(
            order_no=_next_daily_no(db, "LL", IssueOrder.order_no, today),
            employee_id=employee.id, employee_name=employee.name,
            status=ISSUE_OPEN, items_summary=summary, remark=req.remark,
            issued_at=datetime.now(),
        )
        try:
            db.add(order)
            db.flush()
            break
        except IntegrityError:
            db.rollback()
            order = None
    if order is None:
        raise HTTPException(409, "领用单号生成冲突，请重试")

    for material_id, qty in merged.items():
        m = materials[material_id]
        db.add(
            IssueItem(
                issue_order_id=order.id, material_id=m.id,
                material_code=m.code, material_name=m.name, unit=m.unit,
                qty=qty, returned_qty=ZERO, scrapped_qty=ZERO,
            )
        )
        _move(
            db, m, MOVE_ISSUE, qty, d_stock=-qty, d_issued=qty,
            ref_type="issue_order", ref_id=order.id, ref_no=order.order_no,
            employee=employee, remark=req.remark,
        )

    db.commit()
    db.refresh(order)
    return _issue_out(order, _issue_items_out(db, [order]).get(order.id, []))


@router.get("/issues", response_model=list[IssueOrderOut])
def list_issues(
    employee_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """领用单列表。归还流程用 employee_id + status=issued 取「他名下未结清的单」。"""
    q = db.query(IssueOrder)
    if employee_id:
        q = q.filter(IssueOrder.employee_id == employee_id)
    if status:
        q = q.filter(IssueOrder.status == status)
    orders = q.order_by(IssueOrder.issued_at.desc(), IssueOrder.id.desc()).limit(500).all()
    items = _issue_items_out(db, orders)
    return [_issue_out(o, items.get(o.id, [])) for o in orders]


@router.post("/issues/{issue_id}/return", response_model=IssueOrderOut)
def return_issue(issue_id: int, req: ReturnRequest, db: Session = Depends(get_db)):
    """归还 / 报废。行内两列，和不得超过该行未结清数量。

    有报废就生成一张报废单（单头 + 行 + 原因），归还本身不建单据 ——
    归还只是把领用行结清，再开一张单只会让「单据」这个概念贬值。
    """
    order = db.get(IssueOrder, issue_id)
    if not order:
        raise HTTPException(404, f"领用单不存在: #{issue_id}")
    if order.status == ISSUE_CLOSED:
        raise HTTPException(400, "这张领用单已经全部归还")

    employee = db.get(Employee, order.employee_id)
    items = {it.id: it for it in db.query(IssueItem).filter(
        IssueItem.issue_order_id == order.id).all()}

    plans: list[tuple[IssueItem, Decimal, Decimal]] = []
    for line in req.items:
        it = items.get(line.item_id)
        if not it:
            raise HTTPException(400, f"领用行 #{line.item_id} 不属于这张领用单")
        open_qty = _d(it.qty) - _d(it.returned_qty) - _d(it.scrapped_qty)
        back, scrap = _d(line.return_qty), _d(line.scrap_qty)
        if back < ZERO or scrap < ZERO:
            raise HTTPException(400, "归还/报废数量不能为负")
        if back + scrap <= ZERO:
            continue
        if back + scrap > open_qty:
            raise HTTPException(
                400,
                f"「{it.material_name}」未结清只有 {_f(open_qty)}{it.unit}，"
                f"本次归还 {_f(back)} + 报废 {_f(scrap)} 超出",
            )
        plans.append((it, back, scrap))

    if not plans:
        raise HTTPException(400, "没有填写任何归还或报废数量")

    total_scrap = sum((scrap for _, _, scrap in plans), ZERO)
    if total_scrap > ZERO and not req.reason.strip():
        raise HTTPException(400, "有报废数量时必须填写报废原因")

    scrap_order = None
    if total_scrap > ZERO:
        scrap_order = MaterialScrapOrder(
            order_no=_next_daily_no(db, "BF", MaterialScrapOrder.order_no, date.today()),
            source=SCRAP_FROM_ISSUE,
            issue_order_id=order.id, issue_order_no=order.order_no,
            employee_id=order.employee_id, employee_name=order.employee_name,
            reason=req.reason.strip(), created_at=datetime.now(),
        )
        db.add(scrap_order)
        db.flush()

    for it, back, scrap in plans:
        material = _get_material(db, it.material_id)
        if back > ZERO:
            _move(
                db, material, MOVE_RETURN, back, d_stock=back, d_issued=-back,
                ref_type="issue_order", ref_id=order.id, ref_no=order.order_no,
                employee=employee, remark="归还",
            )
            it.returned_qty = _d(it.returned_qty) + back
        if scrap > ZERO:
            _move(
                db, material, MOVE_SCRAP, scrap, d_issued=-scrap, d_scrapped=scrap,
                ref_type="scrap_order", ref_id=scrap_order.id,
                ref_no=scrap_order.order_no, employee=employee,
                remark=f"领用报废：{req.reason.strip()}",
            )
            it.scrapped_qty = _d(it.scrapped_qty) + scrap
            db.add(
                MaterialScrapItem(
                    scrap_order_id=scrap_order.id, material_id=material.id,
                    material_code=material.code, material_name=material.name,
                    unit=material.unit, qty=scrap,
                )
            )

    # 全部行结清才转「已归还」，部分归还在行上体现，单据状态不膨胀
    remaining = [
        _d(it.qty) - _d(it.returned_qty) - _d(it.scrapped_qty) for it in items.values()
    ]
    if all(r <= ZERO for r in remaining):
        order.status = ISSUE_CLOSED
        order.returned_at = datetime.now()

    db.commit()
    db.refresh(order)
    return _issue_out(order, _issue_items_out(db, [order]).get(order.id, []))


# ---------------------------------------------------------------- 报废

@router.post("/scraps", response_model=ScrapOrderOut)
def scrap_stock(req: StockScrapRequest, db: Session = Depends(get_db)):
    """仓管对库存直接报废：存量−、报废+。恒等式不变，但料没被谁领走。"""
    material = _get_material(db, req.material_id)
    qty = _d(req.qty)
    if qty <= ZERO:
        raise HTTPException(400, "报废数量必须大于 0")
    if _d(material.stock_qty) < qty:
        raise HTTPException(
            400,
            f"「{material.name}」存量只有 {_f(material.stock_qty)}{material.unit}，"
            f"不够报废 {_f(qty)}{material.unit}",
        )

    order = MaterialScrapOrder(
        order_no=_next_daily_no(db, "BF", MaterialScrapOrder.order_no, date.today()),
        source=SCRAP_FROM_STOCK, reason=req.reason.strip(), created_at=datetime.now(),
    )
    db.add(order)
    db.flush()
    db.add(
        MaterialScrapItem(
            scrap_order_id=order.id, material_id=material.id,
            material_code=material.code, material_name=material.name,
            unit=material.unit, qty=qty,
        )
    )
    _move(
        db, material, MOVE_SCRAP, qty, d_stock=-qty, d_scrapped=qty,
        ref_type="scrap_order", ref_id=order.id, ref_no=order.order_no,
        remark=f"库存报废：{req.reason.strip()}",
    )
    db.commit()
    db.refresh(order)
    return _scrap_out(db, [order])[0]


def _scrap_out(db: Session, orders: list[MaterialScrapOrder]) -> list[ScrapOrderOut]:
    if not orders:
        return []
    rows = (
        db.query(MaterialScrapItem)
        .filter(MaterialScrapItem.scrap_order_id.in_([o.id for o in orders]))
        .order_by(MaterialScrapItem.id)
        .all()
    )
    grouped: dict[int, list[ScrapItemOut]] = {o.id: [] for o in orders}
    for r in rows:
        grouped[r.scrap_order_id].append(
            ScrapItemOut(
                id=r.id, material_id=r.material_id, material_code=r.material_code,
                material_name=r.material_name, unit=r.unit, qty=_f(r.qty),
            )
        )
    return [
        ScrapOrderOut(
            id=o.id, order_no=o.order_no, source=o.source,
            source_label=SCRAP_SOURCE_LABELS.get(o.source, o.source),
            issue_order_id=o.issue_order_id, issue_order_no=o.issue_order_no,
            employee_id=o.employee_id, employee_name=o.employee_name,
            reason=o.reason, created_at=o.created_at, items=grouped.get(o.id, []),
        )
        for o in orders
    ]


@router.get("/scraps", response_model=list[ScrapOrderOut])
def list_scraps(db: Session = Depends(get_db)):
    orders = (
        db.query(MaterialScrapOrder)
        .order_by(MaterialScrapOrder.created_at.desc(), MaterialScrapOrder.id.desc())
        .limit(500)
        .all()
    )
    return _scrap_out(db, orders)


# ---------------------------------------------------------------- 流水

@router.get("/movements", response_model=list[MovementOut])
def list_movements(
    limit: int = Query(default=2000, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """全部流水，倒序。筛选（类型/员工/日期）在前端做，理由同 list_materials。"""
    rows = (
        db.query(MaterialMovement, Material.code)
        .join(Material, Material.id == MaterialMovement.material_id)
        .order_by(MaterialMovement.created_at.desc(), MaterialMovement.id.desc())
        .limit(limit)
        .all()
    )
    return [
        MovementOut(
            id=mv.id, material_id=mv.material_id, material_code=code,
            material_name=mv.material_name or "", mtype=mv.mtype,
            mtype_label=MOVE_TYPE_LABELS.get(mv.mtype, mv.mtype),
            qty=_f(mv.qty),
            stock_before=_f(mv.stock_before), stock_after=_f(mv.stock_after),
            issued_before=_f(mv.issued_before), issued_after=_f(mv.issued_after),
            scrapped_before=_f(mv.scrapped_before), scrapped_after=_f(mv.scrapped_after),
            ref_type=mv.ref_type, ref_id=mv.ref_id, ref_no=mv.ref_no,
            employee_id=mv.employee_id, employee_name=mv.employee_name,
            remark=mv.remark, created_at=mv.created_at,
        )
        for mv, code in rows
    ]


@router.get("/{material_id}/movements", response_model=list[MovementOut])
def list_material_movements(material_id: int, db: Session = Depends(get_db)):
    material = _get_material(db, material_id)
    rows = (
        db.query(MaterialMovement)
        .filter(MaterialMovement.material_id == material.id)
        .order_by(MaterialMovement.created_at.desc(), MaterialMovement.id.desc())
        .limit(500)
        .all()
    )
    return [
        MovementOut(
            id=mv.id, material_id=mv.material_id, material_code=material.code,
            material_name=mv.material_name or "", mtype=mv.mtype,
            mtype_label=MOVE_TYPE_LABELS.get(mv.mtype, mv.mtype),
            qty=_f(mv.qty),
            stock_before=_f(mv.stock_before), stock_after=_f(mv.stock_after),
            issued_before=_f(mv.issued_before), issued_after=_f(mv.issued_after),
            scrapped_before=_f(mv.scrapped_before), scrapped_after=_f(mv.scrapped_after),
            ref_type=mv.ref_type, ref_id=mv.ref_id, ref_no=mv.ref_no,
            employee_id=mv.employee_id, employee_name=mv.employee_name,
            remark=mv.remark, created_at=mv.created_at,
        )
        for mv in rows
    ]


# ---------------------------------------------------------------- 标签

@router.post("/labels", response_model=LabelPagesOut)
def batch_labels(req: LabelRequest, db: Session = Depends(get_db)):
    """批量出 A4 拼版（4×9=36 张/页），返回每页 PNG 的 data URI 给前端打印。

    为什么不让前端逐张 <img> 再排版：50×30 的标签在 A4 上必须严格对齐，
    浏览器打印的缩放/边距每家不同，很容易印出来偏半毫米而切歪。
    """
    materials = db.query(Material).filter(Material.id.in_(req.ids)).all()
    by_id = {m.id: m for m in materials}
    # 按传入顺序出图，勾选顺序决定贴纸顺序
    ordered = [by_id[i] for i in req.ids if i in by_id]
    if not ordered:
        raise HTTPException(404, "所选物料都不存在")

    pages = render_label_sheet([_label_data(m) for m in ordered])
    return LabelPagesOut(
        pages=["data:image/png;base64," + base64.b64encode(p).decode() for p in pages]
    )


@router.get("/{material_id}/label")
def material_label(material_id: int, db: Session = Depends(get_db)):
    material = _get_material(db, material_id)
    return Response(content=label_png(_label_data(material)), media_type="image/png")


def _label_data(m: Material) -> MaterialLabelData:
    return MaterialLabelData(
        code=m.code, name=m.name, spec=m.spec, unit=m.unit, location=m.location
    )
