from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db, scoped
from app.invoice_storage import (
    MAX_INVOICES_PER_COST,
    delete_invoice_file,
    save_invoice_image,
    url_of,
)
from app.models.cost import CostInvoice, CostRecord
from app.models.inventory import Material
from app.schemas.cost import CostInvoiceOut, CostRecordIn, CostRecordOut, CostRecordUpdate

router = APIRouter(prefix="/api/costs", tags=["costs"])

# 已纳入库存后锁死的字段：它们决定「入了什么、入多少」。
# 单价/供应商/发票/备注/日期仍可改 —— 那些只影响金额统计，不影响账实，
# 事后补录发票不该变成麻烦。
LOCKED_WHEN_STOCKED = ("quantity", "category", "spec")
LOCKED_LABELS = {"quantity": "数量", "category": "类别", "spec": "规格"}


def _invoice_out(inv: CostInvoice) -> CostInvoiceOut:
    return CostInvoiceOut(
        id=inv.id,
        filename=inv.filename,
        size=inv.size,
        content_type=inv.content_type,
        url=url_of(inv.file_path),
        source=inv.source,
        uploaded_by=inv.uploaded_by,
        created_at=inv.created_at,
    )


def invoices_of(db: Session, cost_ids: list[int]) -> dict[int, list[CostInvoiceOut]]:
    """一次把所有成本记录的发票查出来，避免列表页 N+1。

    按 cost_id 分组后返回；调用方拿 .get(id, []) 即可，不用判空。
    """
    if not cost_ids:
        return {}
    rows = (
        db.query(CostInvoice)
        .filter(CostInvoice.cost_id.in_(cost_ids))
        .order_by(CostInvoice.id.asc())
        .all()
    )
    grouped: dict[int, list[CostInvoiceOut]] = {}
    for inv in rows:
        grouped.setdefault(inv.cost_id, []).append(_invoice_out(inv))
    return grouped


def _to_out(
    record: CostRecord,
    material: Material | None = None,
    invoices: list[CostInvoiceOut] | None = None,
) -> CostRecordOut:
    items = invoices or []
    return CostRecordOut(
        id=record.id,
        category=record.category,
        spec=record.spec,
        quantity=record.quantity,
        unit_price=record.unit_price,
        amount=round(record.unit_price * record.quantity, 2),
        supplier=record.supplier,
        invoiced=record.invoiced,
        remark=record.remark,
        purchase_date=record.purchase_date,
        material_id=record.material_id,
        material_code=material.code if material else "",
        material_name=material.name if material else "",
        stocked_qty=float(record.stocked_qty) if record.stocked_qty is not None else None,
        stocked_at=record.stocked_at,
        invoices=items,
        # 已开票却没图 = 纸质票还没拍，正是「只看缺票」要捞的那批
        invoice_missing=bool(record.invoiced and not items),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _material_of(db: Session, record: CostRecord) -> Material | None:
    if not record.material_id:
        return None
    # 用 query 而不是 db.get：get 走的是身份映射/直接加载，不经过
    # do_orm_execute，with_loader_criteria 的 is_test 条件不会生效。
    return db.query(Material).filter(Material.id == record.material_id).first()


def get_cost_or_404(db: Session, record_id: int) -> CostRecord:
    record = db.query(CostRecord).filter(CostRecord.id == record_id).first()
    if not record:
        raise HTTPException(404, f"成本记录不存在: #{record_id}")
    return record


def record_out(db: Session, record: CostRecord) -> CostRecordOut:
    """单条记录 → 出参（带物料名与发票）。web 与小程序共用，免得两边字段长得不一样。"""
    grouped = invoices_of(db, [record.id])
    return _to_out(record, _material_of(db, record), grouped.get(record.id))


def list_cost_records(db: Session) -> list[CostRecordOut]:
    """全部记录，筛选与统计都在前端做（小程序端也复用这个函数）。

    刻意不在这里加日期/类别查询参数：本项目其它列表页都是前端筛选，
    多一层服务端筛选会出现「服务端先过滤掉、前端再筛一遍」的双层过滤问题。
    """
    rows = (
        db.query(CostRecord, Material)
        .outerjoin(Material, Material.id == CostRecord.material_id)
        .order_by(CostRecord.purchase_date.desc(), CostRecord.id.desc())
        .all()
    )
    grouped = invoices_of(db, [record.id for record, _ in rows])
    return [_to_out(record, material, grouped.get(record.id)) for record, material in rows]


def attach_invoice(
    db: Session,
    record: CostRecord,
    data: bytes,
    original_name: str,
    source: str,
    uploaded_by: str,
) -> CostInvoice:
    """校验、落盘、建行。两个端（web / 小程序）共用这一条路径。

    上传即把「已开票」置真：票都到手了还标着未开票，会让「只看缺票」永远捞不到它。
    反过来删掉图不会自动改回未开票 —— 纸质票可能还在财务那里，交给人判断。
    """
    # 数量校验必须用 scoped：只查列/聚合的语句不走 with_loader_criteria，
    # 测试域会把生产域的票也数进来。
    existing = (
        db.query(func.count(CostInvoice.id))
        .filter(scoped(db, CostInvoice), CostInvoice.cost_id == record.id)
        .scalar()
        or 0
    )
    if existing >= MAX_INVOICES_PER_COST:
        raise HTTPException(
            400, f"一笔成本最多挂 {MAX_INVOICES_PER_COST} 张发票，请先删掉不需要的再传"
        )

    meta = save_invoice_image(data, original_name)
    invoice = CostInvoice(
        cost_id=record.id,
        file_path=meta["file_path"],
        filename=meta["filename"],
        size=meta["size"],
        content_type=meta["content_type"],
        source=source,
        uploaded_by=uploaded_by,
    )
    db.add(invoice)
    record.invoiced = True
    db.commit()
    db.refresh(invoice)
    return invoice


def remove_invoice(db: Session, invoice: CostInvoice) -> int:
    """删一张发票：先删库行并提交，再删文件。

    顺序不能反：文件删早了而事务回滚，库里就留下一条指向空气的记录，
    前端会显示成裂图，还得手工清。反过来的代价只是一个孤儿文件。
    """
    cost_id = invoice.cost_id
    file_path = invoice.file_path
    db.delete(invoice)
    db.commit()
    delete_invoice_file(file_path)
    return cost_id


@router.post("", response_model=CostRecordOut)
def create_cost(req: CostRecordIn, db: Session = Depends(get_db)):
    record = CostRecord(**req.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_out(record)


@router.get("", response_model=list[CostRecordOut])
def list_costs(db: Session = Depends(get_db)):
    return list_cost_records(db)


# ⚠ /invoices/{id} 必须声明在 /{record_id} 之前：FastAPI 按声明顺序匹配，
# 反过来的话 DELETE /api/costs/invoices/5 会先命中 /{record_id}，
# 把 "invoices" 当整数解析，直接 422。
@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(CostInvoice).filter(CostInvoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(404, f"发票不存在: #{invoice_id}")
    remove_invoice(db, invoice)
    return {"deleted": invoice_id}


@router.post("/{record_id}/invoices", response_model=CostRecordOut)
async def upload_invoice(
    record_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """上传一张发票图（web 端）。小程序走 /api/mp/costs/{id}/invoices，逻辑同一条。"""
    record = get_cost_or_404(db, record_id)
    attach_invoice(
        db,
        record,
        await file.read(),
        file.filename or "",
        source="web",
        uploaded_by="web",
    )
    return record_out(db, record)


@router.patch("/{record_id}", response_model=CostRecordOut)
def update_cost(record_id: int, req: CostRecordUpdate, db: Session = Depends(get_db)):
    record = get_cost_or_404(db, record_id)
    changes = req.model_dump(exclude_unset=True)

    if record.material_id:
        touched = [f for f in LOCKED_WHEN_STOCKED if f in changes]
        if touched:
            raise HTTPException(
                400,
                "这笔成本已纳入库存，"
                + "、".join(LOCKED_LABELS[f] for f in touched)
                + "不能再改（会与仓库台账对不上），请先到物料管理撤销入库",
            )

    for field, value in changes.items():
        setattr(record, field, value)
    db.commit()
    db.refresh(record)
    return record_out(db, record)


@router.delete("/{record_id}")
def delete_cost(record_id: int, db: Session = Depends(get_db)):
    record = get_cost_or_404(db, record_id)
    if record.material_id:
        raise HTTPException(
            400, "这笔成本已纳入库存，不能删除；请先到物料管理撤销入库"
        )

    # 连图一起真删：软删留着文件的话，「删了又传」会不停堆孤儿文件，
    # 而且隐私上等于没删。先记路径，提交成功后再动磁盘。
    paths = [
        inv.file_path
        for inv in db.query(CostInvoice).filter(CostInvoice.cost_id == record.id).all()
    ]
    db.query(CostInvoice).filter(CostInvoice.cost_id == record.id).delete(
        synchronize_session=False
    )
    db.delete(record)
    db.commit()
    for path in paths:
        delete_invoice_file(path)
    return {"deleted": record_id}
