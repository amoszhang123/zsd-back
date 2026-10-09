import base64
import hashlib
import io
import json
import secrets
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Literal

import qrcode
from fastapi import APIRouter, Depends, File, HTTPException, Header, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import get_db
from app.models.contract import blocked_orders
from app.models.order import (
    PROGRAM_STEP,
    STEP_LABELS,
    Order,
    hours_from_machining_minutes,
    resolve_order_status,
)
from app.models.production import (
    POSITION_ADMIN,
    POSITION_PROGRAMMER,
    POSITION_QC,
    WORK_ORDER_STATUS_DONE,
    Employee,
    OrderContribution,
    OrderProgrammer,
    WorkOrder,
    WorkOrderEmployee,
    WorkOrderItem,
    in_production_order_ids,
)
from app.models.quality import (
    DISPOSITION_COST_LABELS,
    DISPOSITION_TYPE_LABELS,
    STAGES,
    STAGE_LABELS,
    DispositionOwner,
    QcInspection,
    RepairOrder,
    ReworkOrder,
    ScrapOrder,
)
from app.program_sheet import ProgramSheetError, parse_program_sheet
from app.routers.auth import TOKENS
from app.routers.contracts import (
    get_contract,
    import_contract,
    list_contracts,
    update_contract_item_quantity,
    update_contract_status,
)
from app.routers.production import complete_work, list_order_employees, startable_error
from app.routers.quality import pending_qty, stage_pending_map, submit_judgment
from app.routers.quotes import (
    get_quote,
    import_quote,
    list_quotes,
    quote_history,
    update_quote_item,
)
from app.schemas.contract import (
    ContractDetail,
    ContractImport,
    ContractItemQuantityUpdate,
    ContractStatusUpdate,
    ContractSummary,
)
from app.schemas.production import CompleteWorkItem, CompleteWorkRequest, ContributionIn
from app.schemas.quality import OwnerIn, QcJudgmentRequest, Stage
from app.schemas.quote import (
    QuoteDetail,
    QuoteImport,
    QuoteItemHistory,
    QuoteItemOut,
    QuoteItemUpdate,
    QuoteSummary,
)

router = APIRouter(prefix="/api/mp", tags=["mp"])

DISPOSITION_MODELS = {"rework": ReworkOrder, "scrap": ScrapOrder, "repair": RepairOrder}

WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WX_PHONE_URL = "https://api.weixin.qq.com/wxa/business/getuserphonenumber"

_access_token_cache: dict[str, object] = {"token": "", "expires_at": 0.0}


class WxLoginRequest(BaseModel):
    code: str


class WxLoginResponse(BaseModel):
    token: str
    phone: str


class BindRequest(BaseModel):
    phone: str
    id_card_last4: str


class EmployeeQrResponse(BaseModel):
    employee_id: str
    name: str
    qr_image: str


class VerifyQrRequest(BaseModel):
    token: str


class VerifyQrResponse(BaseModel):
    valid: bool
    employee_id: str = ""
    name: str = ""


def _get_mp_user(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = TOKENS.get(token)
    if not info:
        raise HTTPException(401, "未登录")
    return info


def _wx_request(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"请求微信接口失败: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "微信接口返回内容无法解析") from exc


def _get_access_token() -> str:
    if _access_token_cache["token"] and _access_token_cache["expires_at"] > time.time() + 60:
        return str(_access_token_cache["token"])

    data = _wx_request(
        f"{WX_TOKEN_URL}?grant_type=client_credential"
        f"&appid={settings.wx_appid}&secret={settings.wx_app_secret}"
    )
    token = data.get("access_token")
    if not token:
        raise HTTPException(
            502, f"获取微信 access_token 失败: {data.get('errmsg')} (errcode={data.get('errcode')})"
        )

    _access_token_cache["token"] = token
    _access_token_cache["expires_at"] = time.time() + int(data.get("expires_in", 7200))
    return str(token)


def _resolve_wx_phone(code: str) -> str:
    data = _wx_request(f"{WX_PHONE_URL}?access_token={_get_access_token()}", {"code": code})
    if data.get("errcode"):
        raise HTTPException(
            400, f"微信手机号解析失败: {data.get('errmsg')} (errcode={data['errcode']})"
        )

    phone = (data.get("phone_info") or {}).get("purePhoneNumber") or ""
    if not phone:
        raise HTTPException(400, "微信未返回手机号")
    return phone


@router.post("/wx-login", response_model=WxLoginResponse)
def wx_login(req: WxLoginRequest):
    if settings.wx_appid and settings.wx_app_secret:
        phone = _resolve_wx_phone(req.code)
    elif settings.mp_mock_phone:
        phone = settings.mp_mock_phone
    else:
        raise HTTPException(503, "未配置 WX_APPID/WX_APP_SECRET，也未配置 MP_MOCK_PHONE")

    token = secrets.token_hex(24)
    TOKENS[token] = {
        "username": phone,
        "name": phone,
        "role": "employee",
        "phone": phone,
        "expires": datetime.now() + timedelta(hours=24),
    }
    return WxLoginResponse(token=token, phone=phone)


class CheckPhoneRequest(BaseModel):
    phone: str


class CheckPhoneResponse(BaseModel):
    found: bool
    name: str = ""
    employee_id: str = ""
    gender: str = ""
    position: str = ""
    status: str = ""


@router.post("/check-phone", response_model=CheckPhoneResponse)
def check_phone(req: CheckPhoneRequest, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.phone == req.phone).first()
    if not emp:
        return CheckPhoneResponse(found=False)
    return CheckPhoneResponse(
        found=True,
        name=emp.name,
        employee_id=emp.id,
        gender=emp.gender,
        position=emp.position,
        status=emp.status,
    )


@router.post("/bind-employee")
def bind_employee(req: BindRequest, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(
        Employee.phone == req.phone,
        Employee.id_card.like(f"%{req.id_card_last4}")
    ).first()

    if not emp:
        raise HTTPException(400, "手机号与身份证后四位不匹配")

    return {
        "id": emp.id,
        "name": emp.name,
        "gender": emp.gender,
        "position": emp.position,
        "status": emp.status,
        "phone": req.phone,
    }


@router.get("/employee-info")
def get_employee_info(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    phone = user.get("phone", "")
    emp = db.query(Employee).filter(Employee.phone == phone).first()
    if not emp:
        raise HTTPException(404, "未绑定员工")
    return {
        "id": emp.id,
        "name": emp.name,
        "gender": emp.gender,
        "position": emp.position,
        "status": emp.status,
        "phone": phone,
    }


def _render_qr_data_uri(text: str) -> str:
    img = qrcode.make(
        text,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=8,
        border=2,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@router.get("/employee-qr", response_model=EmployeeQrResponse)
def get_employee_qr(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    phone = user.get("phone", "")
    emp = db.query(Employee).filter(Employee.phone == phone).first()
    if not emp:
        raise HTTPException(404, "未绑定员工")

    return EmployeeQrResponse(
        employee_id=emp.id,
        name=emp.name,
        qr_image=_render_qr_data_uri(emp.id),
    )


@router.post("/verify-qr", response_model=VerifyQrResponse)
def verify_qr(req: VerifyQrRequest, db: Session = Depends(get_db)):
    parts_check = req.token
    if len(parts_check) != 32:
        return VerifyQrResponse(valid=False)

    now = int(time.time())
    emps = db.query(Employee).all()
    for emp in emps:
        for offset in range(0, 120, 1):
            ts = now - offset
            raw = f"{emp.id}:{emp.name}:{ts}:factory_qr_secret"
            expected = hashlib.sha256(raw.encode()).hexdigest()[:32]
            if expected == req.token:
                return VerifyQrResponse(valid=True, employee_id=emp.id, name=emp.name)

    return VerifyQrResponse(valid=False)


class DispositionLossItem(BaseModel):
    doc_type: str
    type_label: str = ""
    cost_label: str = ""
    doc_id: int
    order_id: str
    product: str = ""
    stage: str = ""
    stage_label: str = ""
    quantity: int = 0
    labor_hours: float = 0.0
    # 报废单这里是物料损失，返工/维修单是额外支出，含义见 cost_label
    material_cost: float = 0.0
    created_at: datetime
    # 同一张单上的其他负责人，便于本人知道是几个人一起担的
    co_owners: list[str] = []


class LossByType(BaseModel):
    doc_type: str
    type_label: str = ""
    cost_label: str = ""
    doc_count: int = 0
    labor_hours: float = 0.0
    material_cost: float = 0.0


class DispositionLossResponse(BaseModel):
    employee_id: str
    name: str = ""
    doc_count: int = 0
    total_quantity: int = 0
    total_labor_hours: float = 0.0
    total_material_cost: float = 0.0
    by_type: list[LossByType] = []
    items: list[DispositionLossItem] = []


@router.get("/disposition-losses", response_model=DispositionLossResponse)
def get_my_disposition_losses(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """当前登录员工涉及的报废 / 返工 / 维修单，以及分摊到他头上的工时与金额。"""
    phone = user.get("phone", "")
    emp = db.query(Employee).filter(Employee.phone == phone).first()
    if not emp:
        raise HTTPException(404, "未绑定员工")

    empty = DispositionLossResponse(employee_id=emp.id, name=emp.name)
    mine = (
        db.query(DispositionOwner)
        .filter(DispositionOwner.employee_id == emp.id)
        .all()
    )
    if not mine:
        return empty

    # 按类型分组取出对应的单据
    ids_by_type: dict[str, list[int]] = {}
    for m in mine:
        ids_by_type.setdefault(m.disposition_type, []).append(m.disposition_id)
    loss_by_key = {(m.disposition_type, m.disposition_id): m for m in mine}

    docs: dict[tuple[str, int], object] = {}
    for doc_type, ids in ids_by_type.items():
        model = DISPOSITION_MODELS.get(doc_type)
        if not model:
            continue
        for d in db.query(model).filter(model.id.in_(ids)).all():
            docs[(doc_type, d.id)] = d
    if not docs:
        return empty

    # 同一张单上的其他负责人
    co: dict[tuple[str, int], list[str]] = {}
    for doc_type, ids in ids_by_type.items():
        rows = (
            db.query(DispositionOwner)
            .options(joinedload(DispositionOwner.employee))
            .filter(
                DispositionOwner.disposition_type == doc_type,
                DispositionOwner.disposition_id.in_(ids),
            )
            .all()
        )
        for o in rows:
            if o.employee_id != emp.id:
                name = o.employee.name if o.employee else o.employee_id
                co.setdefault((doc_type, o.disposition_id), []).append(name)

    items = []
    for key, doc in docs.items():
        doc_type, doc_id = key
        row = loss_by_key.get(key)
        items.append(
            DispositionLossItem(
                doc_type=doc_type,
                type_label=DISPOSITION_TYPE_LABELS.get(doc_type, doc_type),
                cost_label=DISPOSITION_COST_LABELS.get(doc_type, "金额"),
                doc_id=doc_id,
                order_id=doc.order_id,
                product=doc.order.product if doc.order else "",
                stage=doc.stage,
                stage_label=STAGE_LABELS.get(doc.stage, doc.stage),
                quantity=doc.quantity,
                labor_hours=row.labor_hours if row else 0.0,
                material_cost=row.material_cost if row else 0.0,
                created_at=doc.created_at,
                co_owners=co.get(key, []),
            )
        )
    items.sort(key=lambda i: (i.created_at, i.doc_id), reverse=True)

    by_type = []
    for doc_type in sorted(ids_by_type):
        group = [i for i in items if i.doc_type == doc_type]
        if not group:
            continue
        by_type.append(
            LossByType(
                doc_type=doc_type,
                type_label=DISPOSITION_TYPE_LABELS.get(doc_type, doc_type),
                cost_label=DISPOSITION_COST_LABELS.get(doc_type, "金额"),
                doc_count=len(group),
                labor_hours=round(sum(i.labor_hours for i in group), 2),
                material_cost=round(sum(i.material_cost for i in group), 2),
            )
        )

    return DispositionLossResponse(
        employee_id=emp.id,
        name=emp.name,
        doc_count=len(items),
        total_quantity=sum(i.quantity for i in items),
        total_labor_hours=round(sum(i.labor_hours for i in items), 2),
        total_material_cost=round(sum(i.material_cost for i in items), 2),
        by_type=by_type,
        items=items,
    )


# --------------------------------------------------------------- 工单（开工单）


class MpWorkOrderOut(BaseModel):
    """列表项：工单号 + 开工时间 + 完工时间。"""

    id: int
    work_date: date
    status: str
    created_at: datetime
    completed_at: datetime | None = None
    item_count: int = 0


class MpWorkOrderListOut(BaseModel):
    work_orders: list[MpWorkOrderOut] = []
    # 能不能再开一张：名下没有未完工的开工单时为 True。
    # 由后端算好给前端，免得前端自己判断跟建单校验对不上。
    can_start: bool = True
    open_work_order_id: int | None = None


class MpParticipantOut(BaseModel):
    employee_id: str
    employee_name: str = ""


class MpWorkOrderItemOut(BaseModel):
    order_id: str
    order_no: str = ""
    project_no: str = ""
    product: str = ""
    model: str = ""
    quantity: int = 0
    # 本张开工单填的完工数，未完工时为 None
    completed_qty: int | None = None
    # 其它开工单已累计完工的数量，用来算「本次再做多少就够全量」
    already_done: int = 0
    order_status: str = ""
    # 接力生产（同一订单被多名员工做过）时，完工要按人分配参与度
    participants: list[MpParticipantOut] = []


class MpWorkOrderDetailOut(BaseModel):
    id: int
    work_date: date
    status: str
    created_at: datetime
    completed_at: datetime | None = None
    can_scan: bool = False
    can_complete: bool = False
    items: list[MpWorkOrderItemOut] = []


class MpScanRequest(BaseModel):
    # 扫码拿到的原始内容。解析放后端做：图纸码是 JSON，也要容忍扫到裸编号。
    content: str = Field(min_length=1, max_length=500)


class MpCompleteItem(BaseModel):
    order_id: str
    completed_qty: int = Field(default=0, ge=0)
    contributions: list[ContributionIn] | None = None


class MpCompleteRequest(BaseModel):
    items: list[MpCompleteItem] = []


def _current_employee(user, db: Session) -> Employee:
    """token 里只有手机号，员工身份靠手机号反查（与 employee-info 同口径）。"""
    emp = db.query(Employee).filter(Employee.phone == user.get("phone", "")).first()
    if not emp:
        raise HTTPException(404, "未绑定员工")
    return emp


def _owned_work_order(db: Session, work_order_id: int, employee_id: str) -> WorkOrder:
    work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
    if not work_order:
        raise HTTPException(404, f"开工单不存在: #{work_order_id}")
    if employee_id not in [link.employee_id for link in work_order.employees]:
        raise HTTPException(403, f"开工单 #{work_order_id} 不在你名下，无权操作")
    return work_order


def _open_work_order(db: Session, employee_id: str) -> WorkOrder | None:
    return (
        db.query(WorkOrder)
        .join(WorkOrderEmployee, WorkOrderEmployee.work_order_id == WorkOrder.id)
        .filter(
            WorkOrderEmployee.employee_id == employee_id,
            WorkOrder.status != WORK_ORDER_STATUS_DONE,
        )
        .order_by(WorkOrder.id.desc())
        .first()
    )


def _participants_map(db: Session, order_ids: list[str]) -> dict[str, list[MpParticipantOut]]:
    """订单 → 做过的员工。一次查完，避免逐单 N+1。"""
    if not order_ids:
        return {}
    rows = (
        db.query(WorkOrderItem.order_id, Employee.id, Employee.name)
        .join(WorkOrderEmployee, WorkOrderEmployee.work_order_id == WorkOrderItem.work_order_id)
        .join(Employee, Employee.id == WorkOrderEmployee.employee_id)
        .filter(WorkOrderItem.order_id.in_(order_ids))
        .distinct()
        .all()
    )
    out: dict[str, list[MpParticipantOut]] = {}
    for order_id, emp_id, name in rows:
        out.setdefault(order_id, []).append(
            MpParticipantOut(employee_id=emp_id, employee_name=name)
        )
    return out


def _work_order_detail(db: Session, work_order: WorkOrder) -> MpWorkOrderDetailOut:
    items = list(work_order.items)
    order_ids = [i.order_id for i in items]

    orders = (
        {o.id: o for o in db.query(Order).filter(Order.id.in_(order_ids)).all()}
        if order_ids
        else {}
    )
    producing = in_production_order_ids(db, order_ids)
    already: dict[str, int] = {}
    if order_ids:
        already = {
            oid: int(total or 0)
            for oid, total in db.query(
                WorkOrderItem.order_id, func.sum(WorkOrderItem.completed_qty)
            )
            .filter(
                WorkOrderItem.order_id.in_(order_ids),
                WorkOrderItem.work_order_id != work_order.id,
                WorkOrderItem.completed_qty.isnot(None),
            )
            .group_by(WorkOrderItem.order_id)
            .all()
        }
    participants = _participants_map(db, order_ids)

    out_items = []
    for item in items:
        order = orders.get(item.order_id)
        out_items.append(
            MpWorkOrderItemOut(
                order_id=item.order_id,
                order_no=order.order_no if order else "",
                project_no=order.project_no if order else "",
                product=order.product if order else "",
                model=order.model if order else "",
                quantity=order.quantity if order else 0,
                completed_qty=item.completed_qty,
                already_done=already.get(item.order_id, 0),
                order_status=resolve_order_status(order, producing) if order else "",
                participants=participants.get(item.order_id, []),
            )
        )

    unfinished = work_order.status != WORK_ORDER_STATUS_DONE
    return MpWorkOrderDetailOut(
        id=work_order.id,
        work_date=work_order.work_date,
        status=work_order.status,
        created_at=work_order.created_at,
        completed_at=work_order.completed_at,
        can_scan=unfinished,
        can_complete=unfinished,
        items=out_items,
    )


def _parse_scan_candidates(content: str) -> list[str]:
    """从扫码内容里取出能定位订单的候选编号。

    图纸二维码是 orders.get_qrcode 生成的 JSON：
      {"workOrderNo": "WO41765CH", "orderNo": "OD...", "projectNo": "..."}
    注意 workOrderNo 装的是 orders.id（ERP 里显示为工单号），不是 order_no。
    扫到裸编号也认（现场也可能直接手输）。
    """
    text = (content or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        keys = ("workOrderNo", "orderNo", "order_id", "order_no", "id")
        found = [str(data[k]).strip() for k in keys if data.get(k)]
        return found or [text]
    return [text]


def _resolve_scanned_order(db: Session, candidates: list[str]) -> Order:
    """按扫码内容定位订单。

    order.id 是主键、全局唯一，图纸二维码里的 workOrderNo 装的就是它，所以先按 id
    精确匹配，命中即返回。

    order_no 是**采购单号**，同一合同下几百张订单共用一个值（实测 259 张共用
    「2026090324」），所以只有在唯一命中时才接受。这里原先写的是
    `filter(id.in_(c) | order_no.in_(c)).first()` —— OR 会同时匹配到 259 张，
    而 .first() 没有 ORDER BY，扫 WO41765CH 的码实际返回 WO41581AA，
    把一张毫不相干的订单加进了开工单。
    """
    for cand in candidates:
        order = db.query(Order).filter(Order.id == cand).first()
        if order:
            return order

    for cand in candidates:
        matched = db.query(Order).filter(Order.order_no == cand).limit(2).all()
        if len(matched) == 1:
            return matched[0]
        if matched:
            total = (
                db.query(func.count(Order.id)).filter(Order.order_no == cand).scalar() or 0
            )
            raise HTTPException(
                400,
                f"编号「{cand}」是采购单号，对应 {total} 张订单，无法确定扫的是哪一张；"
                f"请扫图纸上的订单二维码，或直接输入工单号",
            )

    raise HTTPException(404, f"二维码里的编号「{candidates[0]}」找不到对应订单")


@router.get("/work-orders", response_model=MpWorkOrderListOut)
def list_my_work_orders(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """我名下的开工单，按开工时间倒序。"""
    emp = _current_employee(user, db)
    work_orders = (
        db.query(WorkOrder)
        # items 要用来算 item_count，不 joinedload 就会逐张单懒加载一次
        .options(joinedload(WorkOrder.items))
        .join(WorkOrderEmployee, WorkOrderEmployee.work_order_id == WorkOrder.id)
        .filter(WorkOrderEmployee.employee_id == emp.id)
        .order_by(WorkOrder.created_at.desc(), WorkOrder.id.desc())
        .all()
    )
    open_wo = next((w for w in work_orders if w.status != WORK_ORDER_STATUS_DONE), None)
    return MpWorkOrderListOut(
        work_orders=[
            MpWorkOrderOut(
                id=w.id,
                work_date=w.work_date,
                status=w.status,
                created_at=w.created_at,
                completed_at=w.completed_at,
                item_count=len(w.items),
            )
            for w in work_orders
        ],
        can_start=open_wo is None,
        open_work_order_id=open_wo.id if open_wo else None,
    )


@router.post("/work-orders", response_model=MpWorkOrderDetailOut)
def create_my_work_order(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """开工：先建一张**空**开工单，订单随后在机台边扫图纸二维码逐个加进来。

    与 web 端 start_work 的差别就在这：web 是先选好订单再开工，所以要求 order_ids
    非空；小程序开工时还没确定做哪几张，故建单不校验订单，订单的阶段/合同校验
    挪到扫码添加那一步做。「名下不能有未完工的单」这条两端一致。
    """
    emp = _current_employee(user, db)
    open_wo = _open_work_order(db, emp.id)
    if open_wo:
        raise HTTPException(
            400,
            f"你还有未完工的开工单 #{open_wo.id}（{open_wo.work_date}），必须先完工结单",
        )

    today = date.today()
    work_order = WorkOrder(work_date=today)
    work_order.employees = [WorkOrderEmployee(employee_id=emp.id, work_date=today)]
    db.add(work_order)
    db.commit()
    db.refresh(work_order)
    return _work_order_detail(db, work_order)


@router.get("/work-orders/{work_order_id}", response_model=MpWorkOrderDetailOut)
def get_my_work_order(
    work_order_id: int, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    emp = _current_employee(user, db)
    return _work_order_detail(db, _owned_work_order(db, work_order_id, emp.id))


@router.post("/work-orders/{work_order_id}/scan", response_model=MpWorkOrderDetailOut)
def scan_add_order(
    work_order_id: int,
    req: MpScanRequest,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """扫图纸二维码，把解析出的订单加进本张开工单。"""
    emp = _current_employee(user, db)
    work_order = _owned_work_order(db, work_order_id, emp.id)
    if work_order.status == WORK_ORDER_STATUS_DONE:
        raise HTTPException(400, f"开工单 #{work_order.id} 已完工，不能再添加订单")

    candidates = _parse_scan_candidates(req.content)
    if not candidates:
        raise HTTPException(400, "二维码内容为空，识别不出订单")

    order = _resolve_scanned_order(db, candidates)

    if any(item.order_id == order.id for item in work_order.items):
        raise HTTPException(400, f"订单 {order.id} 已在本张开工单里，不用重复扫")

    reason = startable_error([order], "加入开工单")
    if reason:
        raise HTTPException(400, reason)

    blocked = blocked_orders(db, [order.id])
    if blocked:
        raise HTTPException(
            400, f"订单 {order.id} 所属合同状态为「{blocked[order.id]}」，不能开工"
        )

    db.add(WorkOrderItem(work_order_id=work_order.id, order_id=order.id))
    db.commit()
    db.refresh(work_order)
    return _work_order_detail(db, work_order)


@router.post("/work-orders/{work_order_id}/complete", response_model=MpWorkOrderDetailOut)
def complete_my_work_order(
    work_order_id: int,
    req: MpCompleteRequest,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """完工。有明细时复用 web 端 complete_work，累计完工数 / 达全量流转质检1 /
    接力参与度那套逻辑不重写第二遍。
    """
    emp = _current_employee(user, db)
    work_order = _owned_work_order(db, work_order_id, emp.id)
    if work_order.status == WORK_ORDER_STATUS_DONE:
        raise HTTPException(400, "该开工单已完工，不能重复提交")

    # 空单直接结掉。误点了开工又没有删除入口，留着会永久卡住开工按钮；
    # 没有明细就没有完工数、也不会触发流转质检，直接置完工是安全的。
    if not work_order.items:
        work_order.status = WORK_ORDER_STATUS_DONE
        work_order.completed_at = datetime.now()
        db.commit()
        db.refresh(work_order)
        return _work_order_detail(db, work_order)

    if not req.items:
        raise HTTPException(400, "请填写各订单的本次完工数（本次没做的填 0）")

    complete_work(
        CompleteWorkRequest(
            work_order_id=work_order.id,
            employee_id=emp.id,
            items=[
                CompleteWorkItem(
                    order_id=i.order_id,
                    completed_qty=i.completed_qty,
                    contributions=i.contributions,
                )
                for i in req.items
            ],
        ),
        db,
    )
    db.refresh(work_order)
    return _work_order_detail(db, work_order)


# ------------------------------------------------------------------- 我的订单

PROGRAM_STATE_PENDING = "待编程"
PROGRAM_STATE_DONE = "已编程"
QC_STATE_PENDING = "待质检"
QC_STATE_DONE = "已质检"


class MpMyOrderOut(BaseModel):
    """「我的订单」列表项：订单号 + 订单状态 + 能不能编辑。"""

    order_id: str
    order_no: str = ""
    product: str = ""
    model: str = ""
    quantity: int = 0
    # 流水线状态：齐备 / 待生产 / 生产中 / 质检中 …
    status: str = ""
    # 编程维度：待编程 / 已编程 / ""（还没走到程序阶段）
    program_state: str = ""
    # 质检维度：待质检 / 已质检 / ""（非质检岗、或这单与质检无关）
    qc_state: str = ""
    qc_stage: str = ""
    qc_stage_label: str = ""
    qc_pending_qty: int = 0
    # 列表「订单状态」那一列显示它：编程岗看待编程/已编程、质检岗看待质检/已质检
    # 才有意义，其余情况看流水线状态
    display_status: str = ""
    machining_minutes: float | None = None
    estimated_hours: float = 0
    # True → 小程序里显示绿色的「编辑」按钮
    can_edit: bool = False
    # True → 显示绿色的「判定」按钮
    can_judge: bool = False
    programmers: list[str] = []


class MpMyOrderListOut(BaseModel):
    orders: list[MpMyOrderOut] = []
    is_programmer: bool = False
    is_qc: bool = False
    pending_count: int = 0
    programmed_count: int = 0
    # 质检岗用这两个；编程岗看上面两个
    qc_pending_count: int = 0
    qc_done_count: int = 0


class MpQcStageOut(BaseModel):
    """某个质检阶段的待检情况。前端据此决定判定页里能选哪些阶段。"""

    stage: str
    label: str = ""
    upstream_qty: int = 0
    inspected_qty: int = 0
    pending_qty: int = 0


class MpOwnerCandidateOut(BaseModel):
    """可选的工单负责人（做过这张订单的员工），带出参与度帮着分配损耗。"""

    employee_id: str
    employee_name: str = ""
    percent: int | None = None


class MpInspectionOut(BaseModel):
    """这张订单上已有的一条质检批次记录。"""

    id: int
    stage: str
    stage_label: str = ""
    quantity: int = 0
    pass_count: int = 0
    fail_count: int = 0
    scrap_qty: int = 0
    verdict: str = ""
    remark: str = ""
    inspector_id: str | None = None
    inspector_name: str = ""
    # 是不是当前登录的质检员做的
    is_mine: bool = False
    created_at: datetime | None = None


class MpOrderDetailOut(BaseModel):
    """小程序端的订单详情。

    **刻意不含客户名称、单价、金额等商务字段** —— 小程序是给车间员工用的，
    这些不该让他们看到。注意不能只在前端不渲染：接口照样返回的话，抓包或
    开发者工具里就直接看到了，所以从 schema 层就不给。
    """

    order_id: str
    order_no: str = ""
    project_no: str = ""
    material_no: str = ""
    product: str = ""
    model: str = ""
    quantity: int = 0
    priority: str = ""
    order_type: str = ""
    status: str = ""
    program_state: str = ""
    qc_state: str = ""
    create_date: date | None = None
    deadline: date | None = None
    machining_minutes: float | None = None
    estimated_hours: float = 0
    can_edit: bool = False
    can_judge: bool = False
    # 待检的那个阶段，判定页默认用它
    qc_stage: str = ""
    qc_stage_label: str = ""
    qc_pending_qty: int = 0
    programmers: list[str] = []
    qc_stages: list[MpQcStageOut] = []
    owner_candidates: list[MpOwnerCandidateOut] = []
    inspections: list[MpInspectionOut] = []


class MpJudgmentRequest(BaseModel):
    """小程序端的质检判定入参，字段与 web 端 QcJudgmentDialog 提交的一致。

    比 QcJudgmentRequest 少了 order_id（走路径）和 inspector_id（后端从登录态取）。
    """

    stage: Stage
    quantity: int | None = Field(default=None, gt=0)
    scrap_qty: int = Field(default=0, ge=0)
    verdict: Literal["pass", "rework", "repair"] = "pass"
    remark: str = ""
    owners: list[OwnerIn] = []
    return_owners: list[OwnerIn] = []


class MpJudgmentResultOut(BaseModel):
    """判定结果 + 刷新后的订单详情，省一次往返。"""

    message: str = ""
    qc_status: str = ""
    inspected_qty: int = 0
    scrap_qty: int = 0
    passed_qty: int = 0
    rejected_qty: int = 0
    pending_qty: int = 0
    returned_to_production: bool = False
    created_docs: list[str] = []
    detail: MpOrderDetailOut


class MpProgramTimeRequest(BaseModel):
    # 程序时间（分钟），落到 orders.machining_minutes；预估工时由后端按 ÷60÷0.8 算
    machining_minutes: float = Field(ge=0, le=10_000_000)


class MpProgramSheetRowOut(BaseModel):
    name: str
    minutes: float


class MpProgramSheetOut(BaseModel):
    """图纸解析结果。只回算出来的数、不落库 —— 员工核对后再调 program-time 保存。"""

    total_minutes: float
    estimated_hours: float
    sheet_name: str = ""
    rows: list[MpProgramSheetRowOut] = []


def _program_state(order: Order) -> str:
    """待编程 / 已编程 / ""。

    判定口径：停在程序阶段(step 2)且还没填过程序时间 → 待编程；
    填过了、或已推进到投产及之后 → 已编程；还没走到程序阶段 → 空串。
    """
    if order.current_step == PROGRAM_STEP:
        return PROGRAM_STATE_PENDING if order.machining_minutes is None else PROGRAM_STATE_DONE
    if order.current_step > PROGRAM_STEP:
        return PROGRAM_STATE_DONE
    return ""


def _can_edit_program(order: Order) -> bool:
    """只有「待编程」的订单给出编辑入口（小程序里那个绿色按钮）。

    已经填过的不在手机上改：两个编程员同时开着同一张单时，后保存的会静默盖掉
    前一个人的数。要改走 web 端程序管理，那边有全局视角。
    """
    return order.current_step == PROGRAM_STEP and order.machining_minutes is None


def _require_programmer(emp: Employee) -> None:
    if emp.position != POSITION_PROGRAMMER:
        raise HTTPException(
            403,
            f"只有「{POSITION_PROGRAMMER}」岗位可以填写程序时间，"
            f"你当前岗位是「{emp.position}」",
        )


def _require_qc(emp: Employee) -> None:
    if emp.position != POSITION_QC:
        raise HTTPException(
            403,
            f"只有「{POSITION_QC}」岗位可以做质检判定，你当前岗位是「{emp.position}」",
        )


def _qc_pending_by_order(db: Session) -> dict[str, tuple[str, int]]:
    """订单 → (待检阶段, 待检数量)。

    口径直接复用 quality.stage_pending_map，也就是 web 端待检队列用的那套
    （待检 = 上游产出 − 本阶段已检），两端不会出现「一边算待检一边算检完」。
    流水线是顺序的，一张订单正常只有一个阶段在等检；真出现多个时取靠前的那个
    —— STAGES 本身就按流水线顺序排。
    """
    out: dict[str, tuple[str, int]] = {}
    for stage in STAGES:
        for order_id, qty in stage_pending_map(db, stage).items():
            out.setdefault(order_id, (stage, qty))
    return out


def _my_inspected_order_ids(db: Session, employee_id: str) -> set[str]:
    """我作为质检员检过的订单。按 inspector_id 查，不是 employee_id ——
    后者存的是报废/返工的第一位负责人，跟谁做的质检没关系。"""
    rows = (
        db.query(QcInspection.order_id)
        .filter(QcInspection.inspector_id == employee_id)
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def _remember_programmer(db: Session, order_id: str, employee_id: str) -> None:
    """记下「这人编程过这张单」。(order_id, employee_id) 有唯一约束，重复保存不再插。"""
    exists = (
        db.query(OrderProgrammer.id)
        .filter(
            OrderProgrammer.order_id == order_id,
            OrderProgrammer.employee_id == employee_id,
        )
        .first()
    )
    if not exists:
        db.add(OrderProgrammer(order_id=order_id, employee_id=employee_id))


def _programmers_map(db: Session, order_ids: list[str]) -> dict[str, list[str]]:
    """订单 → 参与编程的人名，按首次参与先后排。一次查完，避免逐单 N+1。"""
    if not order_ids:
        return {}
    rows = (
        db.query(OrderProgrammer.order_id, Employee.name)
        .join(Employee, Employee.id == OrderProgrammer.employee_id)
        .filter(OrderProgrammer.order_id.in_(order_ids))
        .order_by(OrderProgrammer.created_at, OrderProgrammer.id)
        .all()
    )
    out: dict[str, list[str]] = {}
    for order_id, name in rows:
        out.setdefault(order_id, []).append(name)
    return out


def _my_order_ids(
    db: Session, emp: Employee, qc_pending: dict[str, tuple[str, int]] | None = None
) -> list[str]:
    """「我的订单」的取数口径，按岗位分三种。

    编程岗：所有待编程的订单（还没人接的活，谁都能领）+ 我参与编程过的订单。
    质检岗：所有待质检的订单 + 我质检过的订单。
    其它岗：我参与生产过的订单（order_contributions 里有我的记录）。
    """
    if emp.position == POSITION_PROGRAMMER:
        pending = [
            row[0]
            for row in db.query(Order.id)
            .filter(
                Order.current_step == PROGRAM_STEP,
                Order.machining_minutes.is_(None),
            )
            .all()
        ]
        programmed = [
            row[0]
            for row in db.query(OrderProgrammer.order_id)
            .filter(OrderProgrammer.employee_id == emp.id)
            .all()
        ]
        # 两个集合可能重叠（我刚编程完、它还停在程序阶段），去重保序
        return list(dict.fromkeys(pending + programmed))

    if emp.position == POSITION_QC:
        # qc_pending 允许调用方传进来复用，列表接口已经算过一遍了，别再算第二次
        wait = qc_pending if qc_pending is not None else _qc_pending_by_order(db)
        return list(dict.fromkeys(list(wait) + sorted(_my_inspected_order_ids(db, emp.id))))

    return [
        row[0]
        for row in db.query(OrderContribution.order_id)
        .filter(OrderContribution.employee_id == emp.id)
        .all()
    ]


def _visible_order(db: Session, order_id: str, emp: Employee) -> Order:
    """取订单，并确认它在「我的订单」范围内 —— 否则拿工单号就能遍历全厂订单。"""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, f"订单不存在: {order_id}")
    if order_id not in _my_order_ids(db, emp):
        raise HTTPException(403, f"订单 {order_id} 不在你的「我的订单」范围内")
    return order


def _order_detail(db: Session, order: Order, emp: Employee) -> MpOrderDetailOut:
    status = resolve_order_status(order, in_production_order_ids(db, [order.id]))
    is_qc = emp.position == POSITION_QC

    qc_stage = ""
    qc_pending_qty = 0
    qc_stages: list[MpQcStageOut] = []
    qc_state = ""
    owner_candidates: list[MpOwnerCandidateOut] = []
    if is_qc:
        # 三个阶段的待检情况都带上，判定页据此列出可选阶段（通常只有一个有待检）
        for stage in STAGES:
            upstream, left = pending_qty(db, order.id, stage)
            qc_stages.append(
                MpQcStageOut(
                    stage=stage,
                    label=STAGE_LABELS[stage],
                    upstream_qty=upstream,
                    inspected_qty=max(0, upstream - left),
                    pending_qty=left,
                )
            )
            if not qc_stage and left > 0:
                qc_stage, qc_pending_qty = stage, left
        owner_candidates = [
            MpOwnerCandidateOut(
                employee_id=c.employee_id, employee_name=c.employee_name, percent=c.percent
            )
            for c in list_order_employees(order.id, db)
        ]

    batches = (
        db.query(QcInspection)
        .filter(QcInspection.order_id == order.id)
        .order_by(QcInspection.created_at, QcInspection.id)
        .all()
    )
    if is_qc:
        if qc_pending_qty > 0:
            qc_state = QC_STATE_PENDING
        elif any(b.inspector_id == emp.id for b in batches):
            qc_state = QC_STATE_DONE

    return MpOrderDetailOut(
        order_id=order.id,
        order_no=order.order_no,
        project_no=order.project_no,
        material_no=order.material_no,
        product=order.product,
        model=order.model,
        quantity=order.quantity,
        priority=order.priority,
        order_type=order.order_type,
        status=status,
        program_state=_program_state(order),
        qc_state=qc_state,
        create_date=order.create_date,
        deadline=order.deadline,
        machining_minutes=order.machining_minutes,
        estimated_hours=order.estimated_hours,
        # 绿色编辑按钮只给编程岗，否则质检员会看到一个点了就 403 的按钮
        can_edit=_can_edit_program(order) and emp.position == POSITION_PROGRAMMER,
        can_judge=is_qc and qc_pending_qty > 0,
        qc_stage=qc_stage,
        qc_stage_label=STAGE_LABELS.get(qc_stage, ""),
        qc_pending_qty=qc_pending_qty,
        programmers=_programmers_map(db, [order.id]).get(order.id, []),
        qc_stages=qc_stages,
        owner_candidates=owner_candidates,
        inspections=[
            MpInspectionOut(
                id=b.id,
                stage=b.stage,
                stage_label=STAGE_LABELS.get(b.stage, b.stage),
                quantity=b.quantity,
                pass_count=b.pass_count,
                fail_count=b.fail_count,
                scrap_qty=b.scrap_qty,
                verdict=b.verdict,
                remark=b.remark,
                inspector_id=b.inspector_id,
                inspector_name=b.inspector.name if b.inspector else "",
                is_mine=b.inspector_id == emp.id,
                created_at=b.created_at,
            )
            for b in batches
        ],
    )


@router.get("/my-orders", response_model=MpMyOrderListOut)
def my_orders(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """我涉及的订单，按岗位分口径：

    编程岗 = 所有待编程 + 我编程过的；质检岗 = 所有待质检 + 我质检过的；
    其它岗 = 我参与生产过的。
    """
    emp = _current_employee(user, db)
    is_programmer = emp.position == POSITION_PROGRAMMER
    is_qc = emp.position == POSITION_QC

    # 待检映射算一次，_my_order_ids 和下面的行构造共用，别重复扫三张表
    qc_pending = _qc_pending_by_order(db) if is_qc else {}
    my_inspected = _my_inspected_order_ids(db, emp.id) if is_qc else set()

    ids = _my_order_ids(db, emp, qc_pending)
    orders = db.query(Order).filter(Order.id.in_(ids)).all() if ids else []
    orders.sort(key=lambda o: (o.create_date or date.min, o.id), reverse=True)

    producing = in_production_order_ids(db, [o.id for o in orders])
    programmers = _programmers_map(db, [o.id for o in orders])

    items = []
    for o in orders:
        status = resolve_order_status(o, producing)
        program_state = _program_state(o) if is_programmer else ""
        qc_state = ""
        qc_stage = ""
        qc_qty = 0
        if is_qc:
            hit = qc_pending.get(o.id)
            if hit:
                qc_state = QC_STATE_PENDING
                qc_stage, qc_qty = hit
            elif o.id in my_inspected:
                qc_state = QC_STATE_DONE
        items.append(
            MpMyOrderOut(
                order_id=o.id,
                order_no=o.order_no,
                product=o.product,
                model=o.model,
                quantity=o.quantity,
                status=status,
                program_state=program_state,
                qc_state=qc_state,
                qc_stage=qc_stage,
                qc_stage_label=STAGE_LABELS.get(qc_stage, ""),
                qc_pending_qty=qc_qty,
                display_status=qc_state or program_state or status,
                machining_minutes=o.machining_minutes,
                estimated_hours=o.estimated_hours,
                can_edit=_can_edit_program(o) and is_programmer,
                can_judge=qc_state == QC_STATE_PENDING,
                programmers=programmers.get(o.id, []),
            )
        )
    return MpMyOrderListOut(
        orders=items,
        is_programmer=is_programmer,
        is_qc=is_qc,
        pending_count=sum(1 for i in items if i.program_state == PROGRAM_STATE_PENDING),
        programmed_count=sum(1 for i in items if i.program_state == PROGRAM_STATE_DONE),
        qc_pending_count=sum(1 for i in items if i.qc_state == QC_STATE_PENDING),
        qc_done_count=sum(1 for i in items if i.qc_state == QC_STATE_DONE),
    )


@router.get("/orders/{order_id}", response_model=MpOrderDetailOut)
def my_order_detail(order_id: str, user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    emp = _current_employee(user, db)
    return _order_detail(db, _visible_order(db, order_id, emp), emp)


@router.post("/orders/{order_id}/program-sheet", response_model=MpProgramSheetOut)
async def upload_program_sheet(
    order_id: str,
    file: UploadFile = File(...),
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """上传钢料设定单图纸（.xls），累加「时间」列算出程序时间。

    只解析、不落库：结果回给前端填进输入框，员工核对无误后再调 program-time 保存。
    这样图纸算出来的数不对还能手动改，不会一上传就直接写进库。
    """
    emp = _current_employee(user, db)
    _require_programmer(emp)
    _visible_order(db, order_id, emp)

    filename = file.filename or ""
    if not filename.lower().endswith(".xls"):
        raise HTTPException(400, f"只支持 .xls 图纸，收到的是「{filename or '未命名文件'}」")

    try:
        result = parse_program_sheet(await file.read())
    except ProgramSheetError as e:
        raise HTTPException(400, str(e)) from e

    return MpProgramSheetOut(
        total_minutes=result.total_minutes,
        estimated_hours=hours_from_machining_minutes(result.total_minutes),
        sheet_name=result.sheet_name,
        rows=[MpProgramSheetRowOut(name=r.name, minutes=r.minutes) for r in result.rows],
    )


@router.put("/orders/{order_id}/program-time", response_model=MpOrderDetailOut)
def save_program_time(
    order_id: str,
    req: MpProgramTimeRequest,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """保存程序时间（分钟）：重算预估工时，并把本人记为该订单的编程参与人。

    阶段限制与 web 端 PATCH /orders/{id}/machining 一致 —— 只有停在程序阶段的
    订单能填，推进到投产后预估工时就不该再变。
    """
    emp = _current_employee(user, db)
    _require_programmer(emp)
    order = _visible_order(db, order_id, emp)

    if order.current_step != PROGRAM_STEP:
        current = resolve_order_status(order, in_production_order_ids(db, [order.id]))
        raise HTTPException(
            409,
            f"只有处于「{STEP_LABELS[PROGRAM_STEP]}」阶段的订单可以填写程序时间，"
            f"{order_id} 当前为「{current}」",
        )

    order.machining_minutes = req.machining_minutes
    order.estimated_hours = hours_from_machining_minutes(req.machining_minutes)
    _remember_programmer(db, order.id, emp.id)
    db.commit()
    db.refresh(order)
    return _order_detail(db, order, emp)


# ------------------------------------------------- 报价管理（仅管理员岗位）


def _require_admin(user, db: Session) -> Employee:
    """取当前登录员工并确认是管理员。

    报价单与合同全是商务数据（单价、总价、甲方、银行账户、税号），
    车间员工一律 403。业务逻辑不重写，各端点直接委托 web 端那套实现，
    这里只负责把门。
    """
    emp = _current_employee(user, db)
    if emp.position != POSITION_ADMIN:
        raise HTTPException(
            403,
            f"只有「{POSITION_ADMIN}」岗位可以访问报价管理，"
            f"你当前岗位是「{emp.position}」",
        )
    return emp


@router.get("/quotes", response_model=list[QuoteSummary])
def mp_list_quotes(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    _require_admin(user, db)
    return list_quotes(db)


@router.post("/quotes/import", response_model=QuoteDetail)
def mp_import_quote(
    payload: QuoteImport, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    _require_admin(user, db)
    return import_quote(payload, db)


@router.get("/quotes/{quote_id}", response_model=QuoteDetail)
def mp_get_quote(quote_id: str, user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    _require_admin(user, db)
    return get_quote(quote_id, db)


@router.get("/quotes/{quote_id}/history", response_model=list[QuoteItemHistory])
def mp_quote_history(quote_id: str, user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    _require_admin(user, db)
    return quote_history(quote_id, db)


@router.patch("/quotes/{quote_id}/items/{item_id}", response_model=QuoteItemOut)
def mp_update_quote_item(
    quote_id: str,
    item_id: int,
    req: QuoteItemUpdate,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    _require_admin(user, db)
    return update_quote_item(quote_id, item_id, req, db)


@router.get("/contracts", response_model=list[ContractSummary])
def mp_list_contracts(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    _require_admin(user, db)
    return list_contracts(db)


@router.post("/contracts/import", response_model=ContractDetail)
def mp_import_contract(
    payload: ContractImport, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    _require_admin(user, db)
    return import_contract(payload, db)


@router.get("/contracts/{contract_id}", response_model=ContractDetail)
def mp_get_contract(
    contract_id: str, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    _require_admin(user, db)
    return get_contract(contract_id, db)


@router.patch("/contracts/{contract_id}/status", response_model=ContractDetail)
def mp_update_contract_status(
    contract_id: str,
    req: ContractStatusUpdate,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    _require_admin(user, db)
    return update_contract_status(contract_id, req, db)


@router.patch("/contracts/{contract_id}/items/{item_id}", response_model=ContractDetail)
def mp_update_contract_item(
    contract_id: str,
    item_id: int,
    req: ContractItemQuantityUpdate,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    _require_admin(user, db)
    return update_contract_item_quantity(contract_id, item_id, req, db)


@router.post("/orders/{order_id}/judgment", response_model=MpJudgmentResultOut)
def submit_my_judgment(
    order_id: str,
    req: MpJudgmentRequest,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    """质检判定。判定逻辑全部复用 web 端那个 submit_judgment，不另写一套：
    报废/返工/维修单、损耗负责人、整批打回投产、合格后推进下一阶段，口径完全一致。

    差别只有一个：inspector_id 一律取登录态，不接受前端传 —— 否则能替别人记功，
    「我已质检过的订单」这个列表也就不可信了。
    """
    emp = _current_employee(user, db)
    _require_qc(emp)
    order = _visible_order(db, order_id, emp)

    result = submit_judgment(
        QcJudgmentRequest(
            order_id=order.id,
            stage=req.stage,
            quantity=req.quantity,
            scrap_qty=req.scrap_qty,
            verdict=req.verdict,
            # 与 web 端 QcJudgmentDialog 一致：employee_id 这个历史单选列取第一位负责人
            employee_id=req.owners[0].employee_id if req.owners else None,
            inspector_id=emp.id,
            remark=req.remark,
            owners=req.owners,
            return_owners=req.return_owners,
        ),
        db,
    )
    db.refresh(order)
    return MpJudgmentResultOut(
        message=result.message,
        qc_status=result.qc_status,
        inspected_qty=result.inspected_qty,
        scrap_qty=result.scrap_qty,
        passed_qty=result.passed_qty,
        rejected_qty=result.rejected_qty,
        pending_qty=result.pending_qty,
        returned_to_production=result.returned_to_production,
        created_docs=result.created_docs,
        detail=_order_detail(db, order, emp),
    )
