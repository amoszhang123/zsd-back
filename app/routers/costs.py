from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.cost import CostRecord
from app.schemas.cost import CostRecordIn, CostRecordOut, CostRecordUpdate

router = APIRouter(prefix="/api/costs", tags=["costs"])


def _to_out(record: CostRecord) -> CostRecordOut:
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
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


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
    records = (
        db.query(CostRecord)
        .order_by(CostRecord.purchase_date.desc(), CostRecord.id.desc())
        .all()
    )
    return [_to_out(r) for r in records]


@router.patch("/{record_id}", response_model=CostRecordOut)
def update_cost(record_id: int, req: CostRecordUpdate, db: Session = Depends(get_db)):
    record = _get_or_404(db, record_id)
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(record, field, value)
    db.commit()
    db.refresh(record)
    return _to_out(record)


@router.delete("/{record_id}")
def delete_cost(record_id: int, db: Session = Depends(get_db)):
    record = _get_or_404(db, record_id)
    db.delete(record)
    db.commit()
    return {"deleted": record_id}
