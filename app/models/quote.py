from datetime import datetime

from sqlalchemy import DateTime, Double, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Quote(Base):
    """报价记录单（本地）。主键由日期 + 时间戳生成，如 BJ20261002-1790526095635。

    报价单只是报价留档，不生成工单；确认接单走原有的接单管理流程。
    合计与明细数都由 items 实时算出，不落库，避免与手填的报价对不上。
    """

    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_file: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    items: Mapped[list["QuoteItem"]] = relationship(
        back_populates="quote", cascade="all, delete-orphan"
    )


class QuoteItem(Base):
    """报价明细。price（报价单价）与 delivery（交期）由人工填写，总价 = price × quantity 实时计算。"""

    __tablename__ = "quote_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quote_id: Mapped[str] = mapped_column(String(40), ForeignKey("quotes.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    category: Mapped[str] = mapped_column(String(100), default="")
    material_no: Mapped[str] = mapped_column(String(50), default="", index=True)
    product: Mapped[str] = mapped_column(String(200), default="", index=True)
    model: Mapped[str] = mapped_column(String(100), default="", index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(20), default="")
    brand: Mapped[str] = mapped_column(String(100), default="")
    price: Mapped[float | None] = mapped_column(Double, nullable=True)
    delivery: Mapped[str] = mapped_column(String(50), default="")

    quote: Mapped["Quote"] = relationship(back_populates="items")
