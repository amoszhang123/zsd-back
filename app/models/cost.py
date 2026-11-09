from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Double, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantScoped


class CostRecord(TenantScoped, Base):
    """成本支出记录。

    category 是自由文本：前端提供 刀具/油品/材料/劳保/备品/工具 预设，选「其他」时手输，
    所以后端不做枚举约束，只要求非空。金额不落库，一律按 单价 × 数量 实时算，
    与出货单、报价单的做法一致，避免改价后汇总对不上。

    继承 TenantScoped 是比较晚才补上的（原先只有 Base）：小程序对管理员开放成本管理后，
    测试账号（微信审核用）会直接看到真实工厂支出，而且它新建的记录会混进真实账目。
    补列由 ensure_schema() 完成，存量行默认 0 = 生产域，web 端行为不变。
    """

    __tablename__ = "cost_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    spec: Mapped[str] = mapped_column(String(200), default="")
    # 数量用 Double：油品/材料常有 2.5 升、1.5 公斤这类小数；
    # 默认的 Float 是单精度，录 1.2 会被存成 1.2000000476837158
    quantity: Mapped[float] = mapped_column(Double, default=0.0)
    unit_price: Mapped[float] = mapped_column(Double, default=0.0)
    supplier: Mapped[str] = mapped_column(String(200), default="", index=True)
    invoiced: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    remark: Mapped[str] = mapped_column(String(500), default="")
    purchase_date: Mapped[date] = mapped_column(Date, index=True)

    # —— 纳入物料库存（见 routers/materials.py 的 stock-in 接口）——
    # 入库的唯一来源就是这里：一条成本记录最多只能纳入一次，撤销后这三列清空。
    # stocked_qty 单独存而不是直接用 quantity：以后若允许「一条成本只纳一部分」，
    # 库存数量与成本数量就会分叉，提前留一列免得那时改表。
    material_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    stocked_qty: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    stocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class CostInvoice(TenantScoped, Base):
    """一张成本记录下的发票图片。

    做成独立表而不是在 cost_records 上挂一列：一笔采购经常是「专票 + 销货清单」
    两张，一张发票也可能对应多笔成本，一列装不下且以后没法加。

    is_test 跟父表一致（也继承 TenantScoped）：直接按 id 删某张票时，
    全局过滤器会自动带上数据域，测试账号不可能删到生产域的票。

    file_path 是相对 uploads 根的路径（invoices/2026/11/<uuid>.jpg），
    不存绝对路径 —— 换机器、换挂载点时库里的数据不用改。
    """

    __tablename__ = "cost_invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cost_id: Mapped[int] = mapped_column(Integer, index=True)

    file_path: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    content_type: Mapped[str] = mapped_column(String(50), default="")

    # 谁传的：小程序记工号（可追责），web 端没有登录态只能记 "web"
    source: Mapped[str] = mapped_column(String(10), default="web")
    uploaded_by: Mapped[str] = mapped_column(String(50), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
