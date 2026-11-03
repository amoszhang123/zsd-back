from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import TenantScoped, get_db, scoped
from app.models.inventory import IssueOrder, MaterialMovement, MaterialScrapOrder
from app.models.production import (
    Employee,
    OrderContribution,
    OrderProgrammer,
    WorkOrderEmployee,
)
from app.models.quality import (
    DispositionOwner,
    QcInspection,
    RepairOrder,
    ReworkOrder,
    ScrapOrder,
)
from app.schemas.employee import EmployeeCreate, EmployeeResponse, EmployeeUpdate

router = APIRouter(prefix="/api/employees", tags=["employees"])

# 姓名、身份证号、手机号都是识别「同一个人」的关键信息：重名会让派工、质检记到
# 别人头上，手机号重复会让小程序按手机号核验身份时串号，所以建员工时必须唯一。
# 身份证和手机号允许不填（落库是空串），空值之间不算冲突。
UNIQUE_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "姓名"),
    ("id_card", "身份证号"),
    ("phone", "手机号"),
)


def _norm(field: str, value: object) -> str:
    """入库前统一格式，避免「多一个空格」「身份证末位小写 x」被当成另一个人。"""
    text = str(value or "").strip()
    return text.upper() if field == "id_card" else text


def _assert_unique(
    db: Session, values: dict[str, object], exclude_id: str | None = None
) -> None:
    """姓名 / 身份证 / 手机号查重，命中就抛 409，把冲突落在哪个员工身上说清楚。

    只校验本次提交里出现的字段（update 用 exclude_unset 传进来），
    空值直接跳过 —— 大量老员工的身份证、手机号本来就是空的。
    """
    for field, label in UNIQUE_FIELDS:
        if field not in values:
            continue
        value = _norm(field, values[field])
        if not value:
            continue
        q = db.query(Employee).filter(getattr(Employee, field) == value)
        if exclude_id:
            q = q.filter(Employee.id != exclude_id)
        dup = q.first()
        if dup:
            raise HTTPException(
                409,
                f"{label}已被员工 {dup.id}（{dup.name}）占用，请核对后再提交",
            )


def _count(db: Session, model, *criteria) -> int:
    """统计员工在业务表里的引用数。

    count() 这类只查聚合、不加载实体的语句不会走全局的 with_loader_criteria，
    测试数据域必须用 scoped() 手工补条件，否则测试号会数到生产数据。
    """
    q = db.query(model).filter(*criteria)
    if issubclass(model, TenantScoped):
        q = q.filter(scoped(db, model))
    return q.count()


# 员工工号被这些业务表引用。有记录就不能物理删除：删掉会让历史开工单、质检单
# 指向一个查不到的人，报表和追溯都断链。这类员工应当把状态改成「离职」。
def _reference_counts(db: Session, employee_id: str) -> dict[str, int]:
    checks: list[tuple[str, int]] = [
        ("开工单", _count(db, WorkOrderEmployee, WorkOrderEmployee.employee_id == employee_id)),
        ("订单参与度", _count(db, OrderContribution, OrderContribution.employee_id == employee_id)),
        ("编程分配", _count(db, OrderProgrammer, OrderProgrammer.employee_id == employee_id)),
        ("返工单", _count(db, ReworkOrder, ReworkOrder.employee_id == employee_id)),
        ("报废单", _count(db, ScrapOrder, ScrapOrder.employee_id == employee_id)),
        ("维修单", _count(db, RepairOrder, RepairOrder.employee_id == employee_id)),
        ("不良归属", _count(db, DispositionOwner, DispositionOwner.employee_id == employee_id)),
        (
            "质检记录",
            _count(
                db,
                QcInspection,
                (QcInspection.employee_id == employee_id)
                | (QcInspection.inspector_id == employee_id),
            ),
        ),
        ("物料流水", _count(db, MaterialMovement, MaterialMovement.employee_id == employee_id)),
        ("领用单", _count(db, IssueOrder, IssueOrder.employee_id == employee_id)),
        (
            "报废领用",
            _count(db, MaterialScrapOrder, MaterialScrapOrder.employee_id == employee_id),
        ),
    ]
    return {label: n for label, n in checks if n}


@router.get("", response_model=list[EmployeeResponse])
def list_employees(
    search: str = Query("", description="搜索姓名/工号"),
    status: str = Query("", description="状态筛选"),
    position: str = Query("", description="岗位筛选"),
    db: Session = Depends(get_db),
):
    q = db.query(Employee)
    if search:
        pattern = f"%{search}%"
        q = q.filter((Employee.name.ilike(pattern)) | (Employee.id.ilike(pattern)))
    if status:
        q = q.filter(Employee.status == status)
    if position:
        q = q.filter(Employee.position == position)
    return q.order_by(Employee.id).all()


@router.get("/{employee_id}", response_model=EmployeeResponse)
def get_employee(employee_id: str, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(404, "员工不存在")
    return emp


@router.post("", response_model=EmployeeResponse)
def create_employee(data: EmployeeCreate, db: Session = Depends(get_db)):
    payload = data.model_dump()
    payload["id"] = (data.id or "").strip()
    for field in ("name", "id_card", "phone"):
        payload[field] = _norm(field, payload.get(field))
    if not payload["id"]:
        raise HTTPException(400, "工号不能为空")
    if not payload["name"]:
        raise HTTPException(400, "姓名不能为空")
    if db.query(Employee).filter(Employee.id == payload["id"]).first():
        raise HTTPException(400, f"工号 {payload['id']} 已存在")

    # 查重放在插入前，把「姓名/身份证/手机号重复」变成一句人能看懂的 409，
    # 而不是等数据库唯一索引抛 IntegrityError。
    _assert_unique(db, payload)

    emp = Employee(**payload)
    db.add(emp)
    try:
        db.commit()
    except IntegrityError:
        # 两个管理员同时提交同一个人时，上面的查重会同时通过；兜底把数据库层的
        # 唯一约束错误翻译成同一句提示，避免前端收到 500。
        db.rollback()
        raise HTTPException(409, "员工信息与已有记录冲突，请刷新列表后重试")
    db.refresh(emp)
    return emp


@router.put("/{employee_id}", response_model=EmployeeResponse)
def update_employee(employee_id: str, data: EmployeeUpdate, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(404, "员工不存在")

    payload = data.model_dump(exclude_unset=True)
    for field in ("name", "id_card", "phone"):
        if field in payload:
            payload[field] = _norm(field, payload[field])
    if "name" in payload and not payload["name"]:
        raise HTTPException(400, "姓名不能为空")

    # 改名/换号同样要查重，否则可以绕过新建时的唯一约束把两个员工改成同一个人
    _assert_unique(db, payload, exclude_id=employee_id)

    for k, v in payload.items():
        setattr(emp, k, v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "员工信息与已有记录冲突，请刷新列表后重试")
    db.refresh(emp)
    return emp


@router.delete("/{employee_id}")
def delete_employee(employee_id: str, db: Session = Depends(get_db)):
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(404, "员工不存在")

    refs = _reference_counts(db, employee_id)
    if refs:
        detail = "、".join(f"{label} {n} 条" for label, n in refs.items())
        raise HTTPException(
            409,
            f"该员工已有业务记录（{detail}），不能删除；如已离职，请把状态改为「离职」",
        )

    name = emp.name
    db.delete(emp)
    db.commit()
    return {"deleted": employee_id, "name": name}
