import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Header, Response, UploadFile
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
    POSITION_OPERATOR,
    POSITION_PROGRAMMER,
    POSITION_QC,
    WORK_ORDER_STATUS_DONE,
    Employee,
    Machine,
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
from app.qr_image import render_qr_data_uri
from app.quote_excel import XLSX_MIME, QuoteExportError, build_quote_xlsx, quote_filename
from app.routers.auth import (
    _get_token,
    _set_token,
    cancel_qr_login,
    confirm_qr_login,
    scan_qr_login,
)
from app.routers.contracts import (
    get_contract,
    import_contract,
    list_contracts,
    update_contract_item_quantity,
    update_contract_status,
)
from app.routers.orders import (
    batch_confirm,
    confirm_order,
    create_order,
    get_order,
    list_orders,
    update_order,
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
from app.schemas.order import (
    BatchConfirmRequest,
    BatchConfirmResponse,
    OrderCreate,
    OrderResponse,
    OrderUpdate,
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

WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/stable_token"
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
    # 二维码里承载的签名 token，以及它还能用多少秒。
    # 小程序按 expires_in 倒计时，归零就重新请求本接口换新码。
    token: str = ""
    expires_in: int = 0


class VerifyQrRequest(BaseModel):
    token: str


class VerifyQrResponse(BaseModel):
    valid: bool
    employee_id: str = ""
    name: str = ""


# 员工二维码是「谁在干活」的身份凭证，web 端开工/完工靠扫它确认身份。
# 所以必须短时效 + 签名：编码裸工号的话，拍一张照片就能永久冒充那个人。
QR_TOKEN_TTL = 60     # 小程序上显示的倒计时秒数
QR_TOKEN_GRACE = 10   # 额外宽限：扫码到调 verify-qr 之间有网络往返，
#                     # 卡在第 59 秒扫的码不该在验证时被判过期
QR_TOKEN_LEN = 32


def _mint_qr_token(emp: Employee, ts: int) -> str:
    """签发员工二维码 token：sha256(工号:姓名:签发秒:密钥) 的前 32 位十六进制。"""
    raw = f"{emp.id}:{emp.name}:{ts}:{settings.qr_secret}"
    return hashlib.sha256(raw.encode()).hexdigest()[:QR_TOKEN_LEN]


def _get_mp_user(authorization: str = Header("")):
    token = authorization.replace("Bearer ", "")
    info = _get_token(token)
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
    # 用稳定版接口：POST + JSON；force_refresh=False 保证多实例/多次调用不会互相顶掉 token
    data = _wx_request(
        WX_TOKEN_URL,
        {
            "grant_type": "client_credential",
            "appid": settings.wx_appid,
            "secret": settings.wx_app_secret,
            "force_refresh": False,
        },
    )
    token = data.get("access_token")
    if not token:
        raise HTTPException(
            502,
            f"获取微信 access_token 失败: {data.get('errmsg')} (errcode={data.get('errcode')})，"
            f"appid={settings.wx_appid or '(未配置)'}",
        )
    _access_token_cache["token"] = token
    _access_token_cache["expires_at"] = time.time() + int(data.get("expires_in", 7200))
    return str(token)


def _resolve_wx_phone(code: str) -> str:
    data = _wx_request(f"{WX_PHONE_URL}?access_token={_get_access_token()}", {"code": code})
    if data.get("errcode"):
        # errcode=40013（invalid appid）在这里几乎总是「后端配的 appid 跟小程序不是同一个」：
        # access_token 能拿到说明后端 appid 本身有效，但 code 是另一个小程序签发的。
        # 把后端在用的 appid 一并回给前端，不用登服务器就能判断是哪边配错了。
        raise HTTPException(
            400,
            f"微信手机号解析失败: {data.get('errmsg')} (errcode={data['errcode']})，"
            f"后端使用的 appid={settings.wx_appid or '(未配置)'}",
        )

    phone = (data.get("phone_info") or {}).get("purePhoneNumber") or ""
    if not phone:
        raise HTTPException(400, "微信未返回手机号")
    return phone


@router.post("/wx-login", response_model=WxLoginResponse)
def wx_login(req: WxLoginRequest):
    # 配了 AppID/Secret 就走真实 code2session；没配时用 .env 的 MP_MOCK_PHONE 兜底，
    # 方便开发者工具（测试号跑不通 code2session）与真机调试。
    # 线上（APP_ENV=production）绝不走 mock：否则任何人 POST 一个 code=mock 就能拿到
    # MP_MOCK_PHONE 那名员工的 token，等于把登录整个绕过。
    if settings.wx_appid and settings.wx_app_secret:
        phone = _resolve_wx_phone(req.code)
    elif settings.mp_mock_phone and settings.app_env != "production":
        phone = settings.mp_mock_phone
    elif settings.app_env == "production":
        raise HTTPException(503, "线上未配置 WX_APPID/WX_APP_SECRET，微信登录不可用")
    else:
        raise HTTPException(503, "未配置 WX_APPID/WX_APP_SECRET，也未配置 MP_MOCK_PHONE")

    token = secrets.token_hex(24)
    _set_token(token, {               # 原来 TOKENS[token] = {...}
        "username": phone,
        "name": phone,
        "role": "employee",
        "phone": phone,
        "expires": (datetime.now() + timedelta(hours=24)).isoformat(),
    })
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


@router.get("/employee-qr", response_model=EmployeeQrResponse)
def get_employee_qr(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """签发一张 60 秒时效的员工二维码。

    二维码内容是签名 token 而不是工号，web 端扫到后必须调 /mp/verify-qr 换身份，
    过期即失效。小程序按 expires_in 倒计时，归零重新请求本接口。
    """
    phone = user.get("phone", "")
    emp = db.query(Employee).filter(Employee.phone == phone).first()
    if not emp:
        raise HTTPException(404, "未绑定员工")

    token = _mint_qr_token(emp, int(time.time()))
    return EmployeeQrResponse(
        employee_id=emp.id,
        name=emp.name,
        qr_image=render_qr_data_uri(token),
        token=token,
        expires_in=QR_TOKEN_TTL,
    )


@router.post("/verify-qr", response_model=VerifyQrResponse)
def verify_qr(req: VerifyQrRequest, db: Session = Depends(get_db)):
    """校验员工二维码 token，换出员工身份。web 端扫到码后调这里。

    有效期 QR_TOKEN_TTL 秒，另加 QR_TOKEN_GRACE 秒宽限给「扫码 → 调本接口」的
    网络往返，否则卡在第 59 秒扫的码会在验证时被判过期。

    token 是单向哈希，反推不出工号，只能拿每个员工 × 每个候选秒数去撞。
    员工数 × 70 次 sha256，几十上百人规模是亚毫秒级；真到上万员工再改成
    「二维码里带明文工号、只校验那一个员工」的形式。
    """
    token = (req.token or "").strip()
    if len(token) != QR_TOKEN_LEN:
        return VerifyQrResponse(valid=False)

    now = int(time.time())
    oldest = now - (QR_TOKEN_TTL + QR_TOKEN_GRACE)
    for emp in db.query(Employee).all():
        for ts in range(now, oldest - 1, -1):
            if _mint_qr_token(emp, ts) == token:
                return VerifyQrResponse(valid=True, employee_id=emp.id, name=emp.name)

    return VerifyQrResponse(valid=False)


class MpQrLoginRequest(BaseModel):
    # web 登录二维码里的 challenge，小程序扫码后从 JSON 里取出来
    challenge: str = Field(min_length=16, max_length=64)


class MpQrLoginScanOut(BaseModel):
    """扫码结果：告诉手机上即将登录哪个网页账号，用于确认弹框。"""

    account_name: str
    username: str = ""
    position: str = ""
    employee_name: str = ""


@router.post("/qr-login/scan", response_model=MpQrLoginScanOut)
def mp_qr_login_scan(
    req: MpQrLoginRequest, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """快捷扫码扫到 web 登录码：按扫码人的工种匹配网页账号，标记为待确认。

    工种没有对应网页账号时返回 403，文案由 auth.scan_qr_login 给。
    """
    emp = _current_employee(user, db)
    return MpQrLoginScanOut(**scan_qr_login(req.challenge, emp.phone, emp.name, emp.position))


@router.post("/qr-login/confirm")
def mp_qr_login_confirm(
    req: MpQrLoginRequest, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """手机上点「确认登录」：签发 web 会话，web 端下一次轮询即登录成功。"""
    emp = _current_employee(user, db)
    return {"ok": True, "account_name": confirm_qr_login(req.challenge, emp.phone)}


@router.post("/qr-login/cancel")
def mp_qr_login_cancel(
    req: MpQrLoginRequest, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """手机上点「取消」：web 端立刻显示已取消并换新码，不用干等二维码过期。"""
    emp = _current_employee(user, db)
    cancel_qr_login(req.challenge, emp.phone)
    return {"ok": True}


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
    # 加工机台（订单行级）。为空 = 走老链路开工没关联机台，界面上显示「未关联」。
    machine_id: str | None = None
    machine_name: str = ""
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


class MpQuickScanOut(BaseModel):
    """快捷扫码的分流结果。前端按 action 决定跳转还是弹窗。"""

    position: str
    order_id: str
    order_no: str = ""
    status: str = ""
    current_step: int = 0
    # work_order_added 操作工已把订单加进开工单（现有的或新建的）
    # program          编程，可跳「订单程序时间」页
    # qc               质检，可跳「质检判定」页
    # order_detail     管理员，只读跳「订单详情」页
    # reject           不满足加单/跳转条件，message 为提醒文案
    action: str
    work_order_id: int | None = None
    work_order_is_new: bool = False
    message: str = ""


class MpMachineScanOut(BaseModel):
    """扫机台码的结果：机台信息 + 名下那张未完工开工单。

    前端拿到后进「机台开工」页：上面显示机台，下面列 work_order 里已有的订单
    （可以点选直接绑机台），或再扫订单码。work_order 为 None 表示名下还没有
    未完工单 —— 此时扫到第一个有效订单才会建单，扫完机台就放弃不会留空单。
    """

    machine_id: str
    machine_name: str = ""
    machine_model: str = ""
    work_order: MpWorkOrderDetailOut | None = None


class MpMachineBindRequest(BaseModel):
    machine_id: str = Field(min_length=1, max_length=20)
    # 二选一：点选单里已有订单时直接给 order_id；现场扫订单码时传扫码原文 content，
    # 由后端解析（与 scan_add_order / quick_scan 同一套解析规则，前端不重复实现）。
    order_id: str | None = Field(default=None, max_length=20)
    content: str | None = Field(default=None, max_length=500)


class MpMachineBindOut(BaseModel):
    work_order_id: int
    work_order_is_new: bool = False
    order_id: str
    machine_id: str
    # 覆盖前那一行的机台，前端用来提示「已从 MC0003 改为 MC0001」
    replaced_machine_id: str | None = None
    message: str = ""


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


def _new_work_order(
    db: Session, emp: Employee, order_id: str | None = None, machine_id: str | None = None
) -> WorkOrder:
    """建一张空开工单。订单随后扫码逐个加，建单时不校验订单。

    order_id 给了就顺带带上第一行 —— 机台开工链路要求「扫到第一个有效订单才
    建单」，建单和加第一行是同一个动作，分两步会留下一个可观测的空单窗口。
    """
    today = date.today()
    work_order = WorkOrder(work_date=today)
    work_order.employees = [WorkOrderEmployee(employee_id=emp.id, work_date=today)]
    if order_id:
        work_order.items = [WorkOrderItem(order_id=order_id, machine_id=machine_id)]
    db.add(work_order)
    db.commit()
    db.refresh(work_order)
    return work_order


def _get_or_create_open_work_order(db: Session, emp: Employee) -> tuple[WorkOrder, bool]:
    """快捷扫码用：有未完工的就用现有的，没有就新开一张。返回 (开工单, 是否新建)。"""
    existing = _open_work_order(db, emp.id)
    if existing:
        return existing, False
    return _new_work_order(db, emp), True


def _order_add_error(db: Session, order: Order) -> str | None:
    """与开工单无关的加单校验（订单阶段 / 合同状态），返回错误文案；None 表示通过。"""
    reason = startable_error([order], "加入开工单")
    if reason:
        return reason
    blocked = blocked_orders(db, [order.id])
    if blocked:
        return f"订单 {order.id} 所属合同状态为「{blocked[order.id]}」，不能开工"
    return None


def _work_order_add_error(db: Session, work_order: WorkOrder, order: Order) -> str | None:
    """把订单加进某张开工单前的全部校验，返回错误文案；None 表示可以加。

    /work-orders/{id}/scan 与 /quick-scan 共用这一份，加单规则不允许有两套。
    """
    if work_order.status == WORK_ORDER_STATUS_DONE:
        return f"开工单 #{work_order.id} 已完工，不能再添加订单"
    if any(item.order_id == order.id for item in work_order.items):
        return f"订单 {order.id} 已在本张开工单里，不用重复扫"
    return _order_add_error(db, order)


def _require_operator(emp: Employee) -> None:
    """开工单是操作工的活。前端只藏了入口，接口层必须自己拦 ——
    否则管理员/编程/质检拿到 token 就能替车间开工、完工、改产量。
    只读查看（列表 / 详情）不受此限制。
    """
    if emp.position != POSITION_OPERATOR:
        raise HTTPException(
            403,
            f"只有「{POSITION_OPERATOR}」岗位可以操作开工单，你当前岗位是「{emp.position}」",
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


def _work_order_detail(
    db: Session, work_order: WorkOrder, readonly: bool = False
) -> MpWorkOrderDetailOut:
    # readonly=True（管理员只读查看）时把 can_scan / can_complete 都关掉，
    # 前端按钮由这两个字段驱动，自然就不显示了，不必在前端再判一次岗位
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
                machine_id=item.machine_id,
                machine_name=item.machine.name if item.machine else "",
                already_done=already.get(item.order_id, 0),
                order_status=resolve_order_status(order, producing) if order else "",
                participants=participants.get(item.order_id, []),
            )
        )

    actionable = work_order.status != WORK_ORDER_STATUS_DONE and not readonly
    return MpWorkOrderDetailOut(
        id=work_order.id,
        work_date=work_order.work_date,
        status=work_order.status,
        created_at=work_order.created_at,
        completed_at=work_order.completed_at,
        can_scan=actionable,
        can_complete=actionable,
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


def _work_order_rows(work_orders: list[WorkOrder]) -> list[MpWorkOrderOut]:
    return [
        MpWorkOrderOut(
            id=w.id,
            work_date=w.work_date,
            status=w.status,
            created_at=w.created_at,
            completed_at=w.completed_at,
            item_count=len(w.items),
        )
        for w in work_orders
    ]


@router.get("/work-orders", response_model=MpWorkOrderListOut)
def list_my_work_orders(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """我名下的开工单，按开工时间倒序。

    管理员是只读查看：返回全厂开工单，can_start 恒为 False ——
    前端「新建开工单」按钮由这个字段驱动，所以不会显示出来。
    """
    emp = _current_employee(user, db)
    # items 要用来算 item_count，不 joinedload 就会逐张单懒加载一次
    query = db.query(WorkOrder).options(joinedload(WorkOrder.items))

    if emp.position == POSITION_ADMIN:
        work_orders = query.order_by(
            WorkOrder.created_at.desc(), WorkOrder.id.desc()
        ).all()
        return MpWorkOrderListOut(
            work_orders=_work_order_rows(work_orders),
            can_start=False,
            open_work_order_id=None,
        )

    work_orders = (
        query.join(WorkOrderEmployee, WorkOrderEmployee.work_order_id == WorkOrder.id)
        .filter(WorkOrderEmployee.employee_id == emp.id)
        .order_by(WorkOrder.created_at.desc(), WorkOrder.id.desc())
        .all()
    )
    open_wo = next((w for w in work_orders if w.status != WORK_ORDER_STATUS_DONE), None)
    return MpWorkOrderListOut(
        work_orders=_work_order_rows(work_orders),
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
    _require_operator(emp)
    open_wo = _open_work_order(db, emp.id)
    if open_wo:
        raise HTTPException(
            400,
            f"你还有未完工的开工单 #{open_wo.id}（{open_wo.work_date}），必须先完工结单",
        )

    return _work_order_detail(db, _new_work_order(db, emp))


@router.get("/work-orders/{work_order_id}", response_model=MpWorkOrderDetailOut)
def get_my_work_order(
    work_order_id: int, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    emp = _current_employee(user, db)
    if emp.position == POSITION_ADMIN:
        # 管理员只读查看任意开工单（不在其名下也算），动作开关由 readonly 关掉
        work_order = db.query(WorkOrder).filter(WorkOrder.id == work_order_id).first()
        if not work_order:
            raise HTTPException(404, f"开工单不存在: #{work_order_id}")
        return _work_order_detail(db, work_order, readonly=True)
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
    _require_operator(emp)
    work_order = _owned_work_order(db, work_order_id, emp.id)

    candidates = _parse_scan_candidates(req.content)
    if not candidates:
        raise HTTPException(400, "二维码内容为空，识别不出订单")

    order = _resolve_scanned_order(db, candidates)

    err = _work_order_add_error(db, work_order, order)
    if err:
        raise HTTPException(400, err)

    db.add(WorkOrderItem(work_order_id=work_order.id, order_id=order.id))
    db.commit()
    db.refresh(work_order)
    return _work_order_detail(db, work_order)


@router.post("/quick-scan", response_model=MpQuickScanOut)
def quick_scan(req: MpScanRequest, user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """快捷扫码：扫订单二维码，按岗位分流，前端拿 action 决定跳转还是弹窗。

    操作工 → 把订单加进当前未完工开工单（没有就新开一张）；加单规则与
             /work-orders/{id}/scan 完全一致（同一份 _work_order_add_error）。
    编程   → 订单已到程序阶段才返回 program，否则 reject 让前端弹窗提醒。
    质检   → 订单已有待检阶段才返回 qc，否则 reject。
    管理员 → 只读查看，返回 order_detail，前端跳订单详情；不参与生产。
    程序/质检的放行口径与各目标页面自身一致，避免「跳过去才发现做不了」。
    """
    emp = _current_employee(user, db)
    if emp.position not in (
        POSITION_OPERATOR, POSITION_PROGRAMMER, POSITION_QC, POSITION_ADMIN
    ):
        raise HTTPException(403, f"「{emp.position}」岗位不支持快捷扫码")

    candidates = _parse_scan_candidates(req.content)
    if not candidates:
        raise HTTPException(400, "二维码内容为空，识别不出订单")
    order = _resolve_scanned_order(db, candidates)

    out = MpQuickScanOut(
        position=emp.position,
        order_id=order.id,
        order_no=order.order_no,
        status=resolve_order_status(order, in_production_order_ids(db, [order.id])),
        current_step=order.current_step,
        action="reject",
    )

    if emp.position == POSITION_ADMIN:
        # 管理员不参与生产，扫码只为查看。订单详情对管理员已放开全量可见，
        # 所以这里不需要任何阶段判断，扫到什么就看什么
        out.action = "order_detail"
        return out

    if emp.position == POSITION_OPERATOR:
        # 与开工单无关的校验先做：不通过就别新建开工单，免得留下一张空单
        err = _order_add_error(db, order)
        if err:
            out.message = err
            return out
        work_order, is_new = _get_or_create_open_work_order(db, emp)
        out.work_order_id = work_order.id
        out.work_order_is_new = is_new
        err = _work_order_add_error(db, work_order, order)
        if err:
            out.message = err
            return out
        db.add(WorkOrderItem(work_order_id=work_order.id, order_id=order.id))
        db.commit()
        out.action = "work_order_added"
        out.message = f"订单 {order.id} 已加入{'新建的' if is_new else ''}开工单 #{work_order.id}"
        return out

    if emp.position == POSITION_PROGRAMMER:
        if _program_state(order) == "":
            out.message = f"订单 {order.id} 当前状态「{out.status}」，还没到程序阶段"
            return out
        out.action = "program"
        return out

    # 质检：与质检判定页同口径 —— 有待检数量的阶段才值得跳过去
    detail = _order_detail(db, order, emp)
    if not any(s.pending_qty > 0 for s in detail.qc_stages):
        out.message = f"订单 {order.id} 当前状态「{out.status}」，还没到质检阶段或当前无待检"
        return out
    out.action = "qc"
    return out


# ---------------------------------------------------------------- 机台开工
#
# 链路：工作台「快捷扫码」扫到机台码 → 前端本地认出 type=machine → 进「机台开工」页
# → 页面调 /machine-scan 拿机台 + 名下未完工单 → 点选已有订单或再扫订单码
# → /machine-bind 落库。
#
# 机台不是开工的必需品：老链路（扫订单码）照旧能开工，machine_id 留空。

def _parse_machine_code(content: str) -> str:
    """从扫码内容里取出机台编号；不是机台码就返回空串。

    标签上的内容是 {"type":"machine","machineId":"MC0001"}，type 字段是与订单码
    （{"workOrderNo":...}）区分的唯一依据。也容忍直接扫到裸编号 MC0001 ——
    现场有人会把码抄在纸上，或者用别的方式生成。
    """
    text = (content or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return ""
        if data.get("type") != "machine":
            return ""
        return str(data.get("machineId") or "").strip()
    return text if re.fullmatch(r"MC\d{4,6}", text) else ""


@router.post("/machine-scan", response_model=MpMachineScanOut)
def machine_scan(
    req: MpScanRequest, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """扫机台码：返回机台信息 + 本人名下未完工的开工单（含各订单已绑机台）。"""
    emp = _current_employee(user, db)
    _require_operator(emp)

    code = _parse_machine_code(req.content)
    if not code:
        raise HTTPException(400, "这不是机台码，请扫机台上贴的二维码")

    machine = db.query(Machine).filter(Machine.id == code).first()
    if not machine:
        raise HTTPException(404, f"机台 {code} 不存在，可能是旧标签")
    if not machine.active:
        raise HTTPException(400, f"机台 {machine.id} 已停用，不能开工")

    open_wo = _open_work_order(db, emp.id)
    return MpMachineScanOut(
        machine_id=machine.id,
        machine_name=machine.name,
        machine_model=machine.model,
        work_order=_work_order_detail(db, open_wo) if open_wo else None,
    )


@router.post("/machine-bind", response_model=MpMachineBindOut)
def machine_bind(
    req: MpMachineBindRequest, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """把机台绑到某个订单上。三条分支，全走同一套校验：

    1. 订单已经在本人的未完工单里 → 只改机台（原机台不同则覆盖并在 message 里说明）
    2. 订单不在单里 → 走 _work_order_add_error 校验后加一行，并带上机台
    3. 名下还没有未完工单 → 订单校验通过后**连单带行一起建**，不留空单窗口
    """
    emp = _current_employee(user, db)
    _require_operator(emp)

    machine = db.query(Machine).filter(Machine.id == req.machine_id).first()
    if not machine:
        raise HTTPException(404, f"机台不存在: {req.machine_id}")
    if not machine.active:
        raise HTTPException(400, f"机台 {machine.id} 已停用，不能开工")

    # 扫码原文就地解析成订单号，后面的分支只认 order_id
    order_id = (req.order_id or "").strip()
    if not order_id:
        if _parse_machine_code(req.content or ""):
            raise HTTPException(400, "这是机台码，请扫图纸上的订单二维码")
        candidates = _parse_scan_candidates(req.content or "")
        if not candidates:
            raise HTTPException(400, "请提供订单号，或扫订单二维码")
        order_id = _resolve_scanned_order(db, candidates).id

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, f"订单不存在: {order_id}")

    open_wo = _open_work_order(db, emp.id)
    if open_wo:
        existing = next((i for i in open_wo.items if i.order_id == order.id), None)
        if existing:
            if existing.machine_id == machine.id:
                return MpMachineBindOut(
                    work_order_id=open_wo.id,
                    order_id=order.id,
                    machine_id=machine.id,
                    message=f"订单 {order.id} 已经关联 {machine.id}，不用重复扫",
                )
            replaced = existing.machine_id
            existing.machine_id = machine.id
            db.commit()
            return MpMachineBindOut(
                work_order_id=open_wo.id,
                order_id=order.id,
                machine_id=machine.id,
                replaced_machine_id=replaced,
                message=(
                    f"订单 {order.id} 的机台已改为 {machine.id}"
                    + (f"（原为 {replaced}）" if replaced else "（原来未关联）")
                ),
            )

        err = _work_order_add_error(db, open_wo, order)
        if err:
            raise HTTPException(400, err)
        db.add(
            WorkOrderItem(
                work_order_id=open_wo.id, order_id=order.id, machine_id=machine.id
            )
        )
        db.commit()
        return MpMachineBindOut(
            work_order_id=open_wo.id,
            order_id=order.id,
            machine_id=machine.id,
            message=f"订单 {order.id} 已加入开工单 #{open_wo.id}，机台 {machine.id}",
        )

    # 没有未完工单：先把订单本身的开工条件校验完再建单，避免留下空单
    err = _order_add_error(db, order)
    if err:
        raise HTTPException(400, err)
    work_order = _new_work_order(db, emp, order_id=order.id, machine_id=machine.id)
    return MpMachineBindOut(
        work_order_id=work_order.id,
        work_order_is_new=True,
        order_id=order.id,
        machine_id=machine.id,
        message=f"已新建开工单 #{work_order.id}，订单 {order.id} 关联机台 {machine.id}",
    )


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
    _require_operator(emp)
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
    """「我的订单」的取数口径，按岗位分四种。

    管理员：全厂订单，与「接单管理」同一数据范围。
    编程岗：所有待编程的订单（还没人接的活，谁都能领）+ 我参与编程过的订单。
    质检岗：所有待质检的订单 + 我质检过的订单。
    其它岗：我参与生产过的订单（order_contributions 里有我的记录）。
    """
    if emp.position == POSITION_ADMIN:
        return [row[0] for row in db.query(Order.id).all()]

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
    # 管理员的可见范围就是全厂，不必再把全量 id 拉出来做成员判断
    if emp.position != POSITION_ADMIN and order_id not in _my_order_ids(db, emp):
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

    管理员 = 全厂订单（与「接单管理」同范围）；编程岗 = 所有待编程 + 我编程过的；
    质检岗 = 所有待质检 + 我质检过的；其它岗 = 我参与生产过的。
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


@router.get("/quotes/{quote_id}/export")
def mp_export_quote(quote_id: str, user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """生成报价单 xlsx，作为附件返回。

    小程序用 uni.downloadFile 拿这个 URL，再 uni.openDocument 直接打开。
    版式由 app/quote_excel.py 保证与 web 端 exportQuote.js 一致。
    """
    _require_admin(user, db)
    quote = get_quote(quote_id, db)
    try:
        data = build_quote_xlsx(quote)
    except QuoteExportError as e:
        raise HTTPException(400, str(e)) from e

    filename = quote_filename(quote.id)
    # 文件名含中文，只能用 RFC 5987 的 filename*；直接塞进 filename= 会乱码。
    # 同时给一个 ASCII 兜底名，照顾不认 filename* 的老客户端。
    ascii_name = f"quote-{quote.id}.xlsx"
    return Response(
        content=data,
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{urllib.parse.quote(filename)}"
            ),
            "Content-Length": str(len(data)),
        },
    )


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


# ------------------------------------------------- 接单管理（仅管理员岗位）


class MpAdminOrderOut(BaseModel):
    """接单管理列表项，字段与 web 端接单管理表格的列一一对应。

    额外带上 customer：web 的搜索框支持按客户搜，移动端也是前端过滤，所以得给。
    刻意不含 steps / materials / quality_results —— 777 张工单带上这些嵌套数据是
    882KB，精简到这些字段是 233KB，而列表页根本用不到那些嵌套内容。
    """

    id: str
    order_no: str = ""
    contract_id: str = ""
    project_no: str = ""
    material_no: str = ""
    customer: str = ""
    product: str = ""
    model: str = ""
    quantity: int = 0
    estimated_hours: float = 0
    priority: str = "普通"
    order_type: str = "自主"
    current_step: int = 0
    status: str = ""
    create_date: date | None = None
    deadline: date | None = None


@router.get("/admin/orders", response_model=list[MpAdminOrderOut])
def mp_admin_list_orders(user=Depends(_get_mp_user), db: Session = Depends(get_db)):
    """全部工单（精简字段）。

    一次拉全量、搜索/筛选/分页都放前端，和 web 端接单管理完全同构 ——
    那边也是后端返回全部、前端 Pager 切片，这样筛选和翻页都是零延迟。
    """
    _require_admin(user, db)
    fields = set(MpAdminOrderOut.model_fields)
    return [
        MpAdminOrderOut(**{k: v for k, v in row.items() if k in fields})
        for row in list_orders(search="", priority="", db=db)
    ]


@router.get("/admin/orders/{order_id}", response_model=OrderResponse)
def mp_admin_get_order(
    order_id: str, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """单张工单完整信息。编辑页靠它回填表单（列表接口是精简字段，不够用）。"""
    _require_admin(user, db)
    return get_order(order_id, db)


@router.post("/admin/orders", response_model=OrderResponse)
def mp_admin_create_order(
    data: OrderCreate, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    _require_admin(user, db)
    return create_order(data, db)


@router.put("/admin/orders/{order_id}", response_model=OrderResponse)
def mp_admin_update_order(
    order_id: str,
    data: OrderUpdate,
    user=Depends(_get_mp_user),
    db: Session = Depends(get_db),
):
    _require_admin(user, db)
    return update_order(order_id, data, db)


@router.post("/admin/orders/{order_id}/confirm", response_model=OrderResponse)
def mp_admin_confirm_order(
    order_id: str, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """单张接单。已接过的会被 confirm_order 自己拦下（400 + 当前状态）。"""
    _require_admin(user, db)
    return confirm_order(order_id, db)


@router.post("/admin/orders/batch-confirm", response_model=BatchConfirmResponse)
def mp_admin_batch_confirm(
    req: BatchConfirmRequest, user=Depends(_get_mp_user), db: Session = Depends(get_db)
):
    """批量接单。目标阶段由订单类型 + skip_material 共同决定：
    自主/半外协勾选「跳过备料阶段」（默认）→ 齐备/程序，不勾选 → 备料中；
    全外协恒为质检中，开关无效。
    合同状态非「正常」的、以及不在待确认阶段的都会被跳过并在 skipped 里回报原因。
    """
    _require_admin(user, db)
    return batch_confirm(req, db)


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
