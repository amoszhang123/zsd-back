import base64
import io
import time
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.contract import Contract, ContractItem, blocked_orders
from app.models.order import (
    MATERIAL_PREP_ENABLED,
    ORDER_STATUS_LABELS,
    PROGRAM_STEP,
    STEP_KEYS,
    STEP_LABELS,
    Order,
    OrderStep,
    batch_confirm_target_step,
    confirm_target_step,
    hours_from_machining_minutes,
    resolve_order_status,
)
from app.models.production import in_production_order_ids
from app.models.quality import QcInspection
from app.order_label import LabelData, order_qr_image, render_label
from app.schemas.order import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    BatchConfirmSkipped,
    MachiningUpdate,
    OrderCreate,
    OrderImportRow,
    OrderLabelPagesOut,
    OrderLabelRequest,
    OrderResponse,
    OrderUpdate,
)

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("/flow-config")
def get_flow_config():
    """流程开关的只读配置，给前端决定界面用。

    这里必须声明在 /{order_id} 之前，否则 "flow-config" 会被当成工单号吃掉。

    目前只暴露备料环节：material_prep_enabled=False 时，接单管理要隐藏
    「跳过备料阶段」勾选框、备料管理页只渲染空状态提示；
    后端把这个常量改回 True，前端会自动恢复出勾选框与任务列表。
    """
    return {"material_prep_enabled": MATERIAL_PREP_ENABLED}


def _build_steps(order_id: str) -> list[OrderStep]:
    return [
        OrderStep(
            order_id=order_id,
            step_key=STEP_KEYS[i],
            step_label=STEP_LABELS[i],
            status="processing" if i == 0 else "waiting",
            operator="管理员" if i == 0 else None,
            time=None,
            sort_order=i,
        )
        for i in range(8)
    ]


def _contract_map(db: Session, order_ids: list[str]) -> dict[str, tuple[str, str]]:
    """订单 → (所属合同 id, 合同状态)。工单是通过合同导入产生的才有值。

    带上状态是给前端置灰用的：合同处于暂停/取消/结案时，它的待确认工单不能再接单。
    """
    if not order_ids:
        return {}
    rows = (
        db.query(ContractItem.order_id, ContractItem.contract_id, Contract.status)
        .join(Contract, Contract.id == ContractItem.contract_id)
        .filter(ContractItem.order_id.in_(order_ids))
        .all()
    )
    return {order_id: (contract_id, status) for order_id, contract_id, status in rows}


def _to_response(
    order: Order,
    contract_id: str = "",
    contract_status: str = "",
    quality_results: list | None = None,
    in_production_ids: set[str] | None = None,
) -> dict:
    steps = sorted(order.steps, key=lambda s: s.sort_order) if order.steps else []
    return {
        "id": order.id,
        "order_no": order.order_no,
        "project_no": order.project_no,
        "material_no": order.material_no,
        "customer": order.customer,
        "product": order.product,
        "model": order.model,
        "quantity": order.quantity,
        "estimated_hours": order.estimated_hours,
        "machining_minutes": order.machining_minutes,
        "priority": order.priority,
        "order_type": order.order_type,
        "current_step": order.current_step,
        "status": resolve_order_status(order, in_production_ids),
        "contract_id": contract_id,
        "contract_status": contract_status,
        "create_date": order.create_date,
        "deadline": order.deadline,
        "steps": [
            {"key": s.step_key, "label": s.step_label, "status": s.status, "operator": s.operator, "time": s.time}
            for s in steps
        ],
        "materials": [
            {"name": m.name, "stock": m.stock, "required": m.required, "status": m.status}
            for m in order.materials
        ],
        "quality_results": quality_results or [],
    }


def _quality_results_map(db: Session, order_ids: list[str]) -> dict[str, list[dict]]:
    """订单 → 各阶段质检汇总，由 qc_inspections 批次累计而来（旧 quality_results 表已退役）。"""
    if not order_ids:
        return {}
    batches = (
        db.query(QcInspection)
        .filter(QcInspection.order_id.in_(order_ids))
        .order_by(QcInspection.created_at, QcInspection.id)
        .all()
    )

    grouped: dict[str, dict[str, dict]] = {}
    for b in batches:
        stage = grouped.setdefault(b.order_id, {}).setdefault(
            b.stage, {"pass": 0, "fail": 0, "verdict": "", "remark": ""}
        )
        stage["pass"] += b.pass_count
        stage["fail"] += b.fail_count
        stage["verdict"] = b.verdict
        stage["remark"] = b.remark

    result: dict[str, list[dict]] = {}
    for order_id, stages in grouped.items():
        rows = []
        for stage, s in stages.items():
            total = s["pass"] + s["fail"]
            rows.append(
                {
                    "stage": stage,
                    "pass_count": s["pass"],
                    "fail_count": s["fail"],
                    "rate": round(s["pass"] / total * 100, 1) if total else 0.0,
                    "qc_status": s["verdict"],
                    "remark": s["remark"],
                }
            )
        result[order_id] = rows
    return result


def _respond(db: Session, order: Order) -> dict:
    contract_id, contract_status = _contract_map(db, [order.id]).get(order.id, ("", ""))
    return _to_response(
        order,
        contract_id,
        contract_status,
        _quality_results_map(db, [order.id]).get(order.id, []),
        in_production_order_ids(db, [order.id]),
    )


@router.get("", response_model=list[OrderResponse])
def list_orders(
    search: str = Query("", description="搜索工单号/订单号/项目号/合同号/客户/产品"),
    priority: str = Query("", description="优先级筛选"),
    db: Session = Depends(get_db),
):
    q = db.query(Order).options(joinedload(Order.steps), joinedload(Order.materials), joinedload(Order.quality_results))
    if search:
        pattern = f"%{search}%"
        contract_order_ids = db.query(ContractItem.order_id).filter(
            ContractItem.contract_id.ilike(pattern)
        )
        q = q.filter(
            (Order.id.ilike(pattern))
            | (Order.order_no.ilike(pattern))
            | (Order.project_no.ilike(pattern))
            | (Order.customer.ilike(pattern))
            | (Order.product.ilike(pattern))
            | (Order.id.in_(contract_order_ids))
        )
    if priority:
        q = q.filter(Order.priority == priority)
    orders = q.order_by(Order.create_date.desc()).all()
    ids = [o.id for o in orders]
    contracts = _contract_map(db, ids)
    qualities = _quality_results_map(db, ids)
    producing = in_production_order_ids(db, ids)

    result = []
    for o in orders:
        contract_id, contract_status = contracts.get(o.id, ("", ""))
        result.append(
            _to_response(o, contract_id, contract_status, qualities.get(o.id, []), producing)
        )
    return result


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: str, db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")
    return _respond(db, order)


@router.post("", response_model=OrderResponse)
def create_order(data: OrderCreate, db: Session = Depends(get_db)):
    ts = int(time.time() * 1000)
    order_id = f"WO{ts % 100000:05d}{chr(65 + ts % 26)}{chr(65 + (ts // 26) % 26)}"
    order = Order(
        id=order_id,
        order_no=data.order_no,
        project_no=data.project_no,
        material_no=data.material_no,
        product=data.product,
        model=data.model,
        quantity=data.quantity,
        priority=data.priority,
        current_step=0,
        create_date=date.today(),
    )
    db.add(order)
    db.flush()
    for step in _build_steps(order_id):
        db.add(step)
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/import", response_model=list[OrderResponse])
def import_orders(rows: list[OrderImportRow], db: Session = Depends(get_db)):
    created = []
    base = int(time.time() * 1000)
    for idx, row in enumerate(rows):
        order_id = f"WO{(base + idx) % 100000:05d}{chr(65 + idx % 26)}{chr(65 + (idx // 26) % 26)}"
        order = Order(
            id=order_id,
            order_no=f"OD{base + idx}",
            project_no=f"PJ-{chr(65 + idx % 26)}{100 + idx}",
            material_no=row.material_no,
            product=row.product,
            model=row.model,
            quantity=row.quantity,
            current_step=0,
            create_date=date.today(),
        )
        db.add(order)
        db.flush()
        for step in _build_steps(order_id):
            db.add(step)
        created.append(order)
    db.commit()
    for o in created:
        db.refresh(o)
    return [_to_response(o) for o in created]


@router.post("/batch-confirm", response_model=BatchConfirmResponse)
def batch_confirm(req: BatchConfirmRequest, db: Session = Depends(get_db)):
    """批量接单。整批一个订单类型，外加一个「跳过备料阶段」开关，
    目标阶段由 batch_confirm_target_step 统一裁定：

    备料总开关关闭（现阶段）→ 自主/半外协恒为齐备/程序(step 2)，skip_material 不入判定。
    备料总开关打开：勾选跳过备料（默认）→ 齐备/程序(step 2)；不勾选 → 备料中(step 1)。
    全外协：恒为质检中(step 4)，开关对其无效。
    目标阶段之前的一切阶段一律标记为已完成。
    """
    order_ids = list(dict.fromkeys(req.order_ids))
    orders = db.query(Order).options(joinedload(Order.steps)).filter(Order.id.in_(order_ids)).all()
    by_id = {o.id: o for o in orders}

    target = batch_confirm_target_step(req.order_type, req.skip_material)
    today = date.today()
    confirmed: list[str] = []
    skipped: list[BatchConfirmSkipped] = []
    # 合同状态非「正常」（暂停/取消/结案）的工单不能接单
    blocked = blocked_orders(db, order_ids)
    # 跳过原因里要报出准确状态（待生产 / 生产中），所以这里也算一次
    producing = in_production_order_ids(db, order_ids)

    for order_id in order_ids:
        order = by_id.get(order_id)
        if not order:
            continue
        if order_id in blocked:
            skipped.append(
                BatchConfirmSkipped(
                    order_id=order_id,
                    reason=f"所属合同状态为「{blocked[order_id]}」，不能接单",
                )
            )
            continue
        if order.current_step != 0:
            current = resolve_order_status(order, producing)
            skipped.append(
                BatchConfirmSkipped(
                    order_id=order_id, reason=f"当前状态为「{current}」，只有待确认的工单可以接单"
                )
            )
            continue

        steps = sorted(order.steps, key=lambda s: s.sort_order)
        for idx in range(target):
            steps[idx].status = "completed"
            steps[idx].time = today
        order.current_step = target
        order.order_type = req.order_type
        steps[target].status = "processing"
        confirmed.append(order_id)

    db.commit()

    missing = [oid for oid in order_ids if oid not in by_id]
    parts = [f"成功接单 {len(confirmed)} 张"]
    if skipped:
        parts.append(f"跳过 {len(skipped)} 张")
    if missing:
        parts.append(f"未找到 {len(missing)} 张")
    return BatchConfirmResponse(
        success=True,
        message="，".join(parts) + f"，状态变更为「{ORDER_STATUS_LABELS[target]}」",
        target_status=ORDER_STATUS_LABELS[target],
        confirmed=confirmed,
        skipped=skipped,
        missing=missing,
    )


@router.put("/{order_id}", response_model=OrderResponse)
def update_order(order_id: str, data: OrderUpdate, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "工单不存在")
    update_data = data.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(order, k, v)
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.patch("/{order_id}/machining", response_model=OrderResponse)
def update_machining_minutes(
    order_id: str, req: MachiningUpdate, db: Session = Depends(get_db)
):
    """填写加工时长（分钟），并据此重算预估工时（小时）。

    只有处于「程序」阶段(step=2)的工单可以填：一旦推进到投产，预估工时就不该再变，
    否则已经在跑的进度和工时会互相矛盾。
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, f"工单不存在: {order_id}")

    if order.current_step != PROGRAM_STEP:
        current = resolve_order_status(order, in_production_order_ids(db, [order.id]))
        raise HTTPException(
            409,
            f"只有处于「{STEP_LABELS[PROGRAM_STEP]}」阶段的工单可以填写加工时长，"
            f"{order_id} 当前为「{current}」",
        )

    order.machining_minutes = req.machining_minutes
    order.estimated_hours = hours_from_machining_minutes(req.machining_minutes)
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/{order_id}/advance", response_model=OrderResponse)
def advance_step(order_id: str, db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")
    if order.current_step >= 7:
        raise HTTPException(400, "工单已全部完成")
    steps = sorted(order.steps, key=lambda s: s.sort_order)
    steps[order.current_step].status = "completed"
    steps[order.current_step].time = date.today()
    order.current_step += 1
    steps[order.current_step].status = "processing"
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/{order_id}/confirm", response_model=OrderResponse)
def confirm_order(order_id: str, db: Session = Depends(get_db)):
    """单张接单。

    落点由 confirm_target_step() 裁定：备料总开关关闭时直接进「程序」，
    备料节点标记为已完成；打开则仍停在「备料」。

    所属合同处于暂停/取消/结案时一律拦下（409）—— 与批量接单同口径，
    否则界面上置灰了按钮，直接调接口还是能接单。
    """
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")

    blocked = blocked_orders(db, [order_id])
    if order_id in blocked:
        raise HTTPException(409, f"所属合同状态为「{blocked[order_id]}」，不能接单")

    if order.current_step != 0:
        status = resolve_order_status(order, in_production_order_ids(db, [order.id]))
        raise HTTPException(400, f"该工单已确认接单，当前状态：{status}")

    steps = sorted(order.steps, key=lambda s: s.sort_order)
    target = confirm_target_step()
    today = date.today()
    for idx in range(target):
        steps[idx].status = "completed"
        steps[idx].time = today
    order.current_step = target
    steps[target].status = "processing"
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.post("/{order_id}/allocate-material", response_model=OrderResponse)
def allocate_material(order_id: str, db: Session = Depends(get_db)):
    order = (
        db.query(Order)
        .options(joinedload(Order.steps), joinedload(Order.materials))
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "工单不存在")
    if order.current_step != 1:
        status = resolve_order_status(order, in_production_order_ids(db, [order.id]))
        raise HTTPException(400, f"只有备料中的工单可以领料，当前状态：{status}")

    for material in order.materials:
        material.status = "ready"
        material.stock = max(material.stock, material.required)

    steps = sorted(order.steps, key=lambda s: s.sort_order)
    steps[1].status = "completed"
    steps[1].time = date.today()
    order.current_step = 2
    steps[2].status = "processing"
    db.commit()
    db.refresh(order)
    return _respond(db, order)


@router.get("/{order_id}/qrcode")
def get_qrcode(order_id: str, db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "工单不存在")

    buf = io.BytesIO()
    order_qr_image(order).save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


def _label_seq_map(db: Session, order_ids: list[str]) -> dict[str, str]:
    """order_id → 合同明细序号（同一单命中多条明细时取 seq 最小的一条）。

    批量出图时一次查完，避免 N 张标签查 N 次合同明细。
    """
    if not order_ids:
        return {}
    rows = (
        db.query(ContractItem.order_id, ContractItem.seq)
        .filter(ContractItem.order_id.in_(order_ids))
        .order_by(ContractItem.order_id, ContractItem.seq)
        .all()
    )
    out: dict[str, str] = {}
    for order_id, seq in rows:
        # 单张接口的旧口径是「seq 为 0/None 就当没有」，这里保持一致
        if order_id not in out and seq:
            out[order_id] = str(seq)
    return out


def _label_data(order: Order, seq: str) -> LabelData:
    """标签上的两种「会缺」的字段都在这里收口：

    序号取合同明细 seq，交期取工单 deadline；交期为空时右下角留空，
    前端会提示「未填交期」，不要在这里编个假日期上去。
    """
    return LabelData(
        order_no=order.order_no,
        project_no=order.project_no,
        model=order.model,
        material_no=order.material_no,
        product=order.product,
        quantity=order.quantity,
        seq=seq,
        deadline=f"{order.deadline.month}月{order.deadline.day}日" if order.deadline else "",
    )


@router.get("/{order_id}/label")
def get_label(order_id: str, db: Session = Depends(get_db)):
    """订单码标签 PNG（80×60mm @300DPI），供 web 端接单管理预览与打印。

    右上角序号取合同明细 seq，右下角交期取工单 deadline；
    二维码与 /qrcode 同一份内容，跨「物料编码/物料名称」两行贴在右侧。
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "工单不存在")

    seq = _label_seq_map(db, [order.id]).get(order.id, "")
    try:
        png = render_label(_label_data(order, seq), order_qr_image(order, box_size=1))
    except Exception as e:
        # 未捕获异常会被 Starlette 兜成纯文本 500，前端只能显示
        # 「Request failed with status code 500」，看不出是字体缺失这种可修问题。
        # 这里转成带原因的 500，管理端预览弹窗就能直接显示出来。
        raise HTTPException(500, f"标签渲染失败：{e}") from e
    return StreamingResponse(io.BytesIO(png), media_type="image/png")


@router.post("/labels", response_model=OrderLabelPagesOut)
def batch_labels(req: OrderLabelRequest, db: Session = Depends(get_db)):
    """批量出图：勾 N 张就回 N 页，前端按页插分页符连着打。

    顺序严格按传入的 order_ids 走 —— 调用方按表格里看到的顺序传，打出来就和
    屏幕一致，不会出现「第一张是第五行」这种对不上的情况。

    **全有全无**：只要有一张渲染不出来就整批 500，并把第几张、哪个单号带出来。
    渲染错误多是缺字体 / 模板越界这类环境问题，同一批其余单也不可信；打一半出来
    现场更难查，热敏纸还白烧。
    """
    orders = db.query(Order).filter(Order.id.in_(req.order_ids)).all()
    by_id = {o.id: o for o in orders}
    missing = [oid for oid in req.order_ids if oid not in by_id]
    if missing:
        head = "、".join(missing[:5])
        more = f" 等 {len(missing)} 张" if len(missing) > 5 else ""
        raise HTTPException(404, f"工单不存在：{head}{more}")

    seqs = _label_seq_map(db, req.order_ids)
    pages: list[str] = []
    for idx, oid in enumerate(req.order_ids, start=1):
        order = by_id[oid]
        try:
            png = render_label(
                _label_data(order, seqs.get(oid, "")), order_qr_image(order, box_size=1)
            )
        except Exception as e:
            raise HTTPException(500, f"第 {idx} 张（{oid}）标签渲染失败：{e}") from e
        pages.append("data:image/png;base64," + base64.b64encode(png).decode())

    return OrderLabelPagesOut(pages=pages)

