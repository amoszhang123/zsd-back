"""费用收支接口：非订单的经营性收付款。

与 /api/costs 平行而不合并，原因见 app/models/cash.py 的模块注释：
一个是「买物料」（要入库、要管发票），一个是「花钱/收钱」（凭证图、无库存）。
"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db, scoped
from app.invoice_storage import (
    VOUCHER_DIR,
    delete_invoice_file,
    save_image,
    url_of,
)
from app.models.cash import CashEntry, CashVoucher
from app.schemas.cash import (
    CashEntryIn,
    CashEntryOut,
    CashEntryUpdate,
    CashVoucherOut,
)

router = APIRouter(prefix="/api/cash", tags=["cash"])

# 一笔最多 9 张，与成本发票同口径（前端文案也照抄成本页的）。
MAX_VOUCHERS_PER_ENTRY = 9


def _voucher_out(v: CashVoucher) -> CashVoucherOut:
    return CashVoucherOut(
        id=v.id,
        filename=v.filename,
        size=v.size,
        content_type=v.content_type,
        url=url_of(v.file_path),
        source=v.source,
        uploaded_by=v.uploaded_by,
        created_at=v.created_at,
    )


def vouchers_of(db: Session, entry_ids: list[int]) -> dict[int, list[CashVoucherOut]]:
    """一次把多笔的凭证查出来，避免列表 N+1。"""
    if not entry_ids:
        return {}
    rows = (
        db.query(CashVoucher)
        .filter(CashVoucher.entry_id.in_(entry_ids))
        .order_by(CashVoucher.id.asc())
        .all()
    )
    grouped: dict[int, list[CashVoucherOut]] = {}
    for v in rows:
        grouped.setdefault(v.entry_id, []).append(_voucher_out(v))
    return grouped


def _to_out(entry: CashEntry, vouchers: list[CashVoucherOut] | None = None) -> CashEntryOut:
    return CashEntryOut(
        id=entry.id,
        direction=entry.direction,
        category=entry.category,
        amount=entry.amount or 0.0,
        counterparty=entry.counterparty or "",
        remark=entry.remark or "",
        entry_date=entry.entry_date,
        vouchers=vouchers or [],
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def get_entry_or_404(db: Session, entry_id: int) -> CashEntry:
    entry = db.query(CashEntry).filter(CashEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(404, f"费用记录不存在: #{entry_id}")
    return entry


def list_cash_entries(db: Session) -> list[CashEntryOut]:
    """全部记录，筛选与统计都在前端做 —— 与成本页保持同一套交互，
    避免出现「服务端先筛一遍、前端再筛一遍」的双层过滤。
    """
    rows = (
        db.query(CashEntry)
        .order_by(CashEntry.entry_date.desc(), CashEntry.id.desc())
        .all()
    )
    grouped = vouchers_of(db, [r.id for r in rows])
    return [_to_out(r, grouped.get(r.id)) for r in rows]


@router.get("", response_model=list[CashEntryOut])
def list_cash(db: Session = Depends(get_db)):
    return list_cash_entries(db)


@router.post("", response_model=CashEntryOut)
def create_cash(req: CashEntryIn, db: Session = Depends(get_db)):
    entry = CashEntry(**req.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _to_out(entry)


# ⚠ /vouchers/{id} 必须声明在 /{entry_id} 之前：FastAPI 按声明顺序匹配，
# 否则 DELETE /api/cash/vouchers/5 会先命中 /{entry_id}，把 "vouchers" 当整数解析，422。
@router.delete("/vouchers/{voucher_id}")
def delete_voucher(voucher_id: int, db: Session = Depends(get_db)):
    voucher = db.query(CashVoucher).filter(CashVoucher.id == voucher_id).first()
    if not voucher:
        raise HTTPException(404, f"凭证不存在: #{voucher_id}")
    file_path = voucher.file_path
    db.delete(voucher)
    db.commit()
    # 先提交再删文件：反过来的话事务回滚会留下指向空气的库行（裂图），
    # 而多一个孤儿文件的代价是零。
    delete_invoice_file(file_path)
    return {"deleted": voucher_id}


@router.post("/{entry_id}/vouchers", response_model=CashEntryOut)
async def upload_voucher(
    entry_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    entry = get_entry_or_404(db, entry_id)

    # 计数必须显式带 scoped：只查聚合的语句不走 with_loader_criteria，
    # 测试域会把生产域的凭证也数进来。
    existing = (
        db.query(func.count(CashVoucher.id))
        .filter(scoped(db, CashVoucher), CashVoucher.entry_id == entry.id)
        .scalar()
        or 0
    )
    if existing >= MAX_VOUCHERS_PER_ENTRY:
        raise HTTPException(
            400, f"一笔最多挂 {MAX_VOUCHERS_PER_ENTRY} 张凭证，请先删掉不需要的再传"
        )

    meta = save_image(await file.read(), file.filename or "", VOUCHER_DIR)
    voucher = CashVoucher(
        entry_id=entry.id,
        file_path=meta["file_path"],
        filename=meta["filename"],
        size=meta["size"],
        content_type=meta["content_type"],
        source="web",
        uploaded_by="web",
    )
    db.add(voucher)
    db.commit()
    db.refresh(entry)
    return _to_out(entry, vouchers_of(db, [entry.id]).get(entry.id))


@router.patch("/{entry_id}", response_model=CashEntryOut)
def update_cash(entry_id: int, req: CashEntryUpdate, db: Session = Depends(get_db)):
    entry = get_entry_or_404(db, entry_id)
    for field, value in req.model_dump(exclude_unset=True).items():
        setattr(entry, field, value)
    db.commit()
    db.refresh(entry)
    return _to_out(entry, vouchers_of(db, [entry.id]).get(entry.id))


@router.delete("/{entry_id}")
def delete_cash(entry_id: int, db: Session = Depends(get_db)):
    entry = get_entry_or_404(db, entry_id)
    # 连凭证一起真删，理由同成本发票：留着文件等于没删，反复删传还会堆孤儿文件。
    paths = [
        v.file_path
        for v in db.query(CashVoucher).filter(CashVoucher.entry_id == entry.id).all()
    ]
    db.query(CashVoucher).filter(CashVoucher.entry_id == entry.id).delete(
        synchronize_session=False
    )
    db.delete(entry)
    db.commit()
    for path in paths:
        delete_invoice_file(path)
    return {"deleted": entry_id}
