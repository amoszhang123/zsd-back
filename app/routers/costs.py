from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.cost import CostRecord
from app.models.inventory import Material
from app.schemas.cost import CostRecordIn, CostRecordOut, CostRecordUpdate

router = APIRouter(prefix="/api/costs", tags=["costs"])

# 已纳入库存后锁死的字段：它们决定「入了什么、入多少」。
# 单价/供应商/发票/备注/日期仍可改 —— 那些只影响金额统计，不影响账实，
# 事后补录发票不该变成麻烦。
LOCKED_WHEN_STOCKED = ("quantity", "category", "spec")
LOCKED_LABELS = {"quantity": "数量", "category": "类别", "spec": "规格"}


def _to_out(record: CostRecord, material: Material | None = None) -> CostRecordOut:
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
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _material_of(db: Session, record: CostRecord) -> Material | None:
    return db.get(Material, record.material_id) if record.material_id else None


def _get_or_404(db: Session, record_id: int) -> CostRecord:
    record = db.get(CostRecord, record_id)
    if not record:
        raise HTTPException(404, f"成本记录不存在: #{record_id}")
    return record


@router.post("", response_model=CostRecordOut)
def create_cost(req: CostRecordIn, db: Session = Depends(get_db)):
    record = CostRecord(**req.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return _to_out(record)


@router.get("", response_model=list[CostRecordOut])
def list_costs(db: Session = Depends(get_db)):
    """返回全部记录，筛选与统计都在前端做。

    刻意不在这里加日期/类别查询参数：本项目其它列表页都是前端筛选，
    多一层服务端筛选会出现「服务端先过滤掉、前端再筛一遍」的双层过滤问题。
    """
    rows = (
        db.query(CostRecord, Material)
        .outerjoin(Material, Material.id == CostRecord.material_id)
        .order_by(CostRecord.purchase_date.desc(), CostRecord.id.desc())
        .all()
    )
    return [_to_out(record, material) for record, material in rows]


@router.patch("/{record_id}", response_model=CostRecordOut)
def update_cost(record_id: int, req: CostRecordUpdate, db: Session = Depends(get_db)):
    record = _get_or_404(db, record_id)
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
    return _to_out(record, _material_of(db, record))


@router.delete("/{record_id}")
def delete_cost(record_id: int, db: Session = Depends(get_db)):
    record = _get_or_404(db, record_id)
    if record.material_id:
        raise HTTPException(
            400, "这笔成本已纳入库存，不能删除；请先到物料管理撤销入库"
        )
    db.delete(record)
    db.commit()
    return {"deleted": record_id}
