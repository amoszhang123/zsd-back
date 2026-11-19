"""审计日志表。

放在 models 下是项目惯例：所有表定义集中在 app/models，逻辑放 app/audit.py。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, TenantScoped


class AuditLog(TenantScoped, Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    # 操作者：web 后台是账号（admin），小程序是工号（999991）
    user_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    user_name: Mapped[str] = mapped_column(String(40), default="")
    # web / mp：同一个名字在后台和小程序里可能是两个人，区分开才追得清
    user_kind: Mapped[str] = mapped_column(String(8), default="", index=True)

    # create / update / delete / approve / reject / cancel / login / logout
    action: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 业务模块：orders / contracts / costs / cash / revenue / permissions ...
    module: Mapped[str] = mapped_column(String(24), default="", index=True)

    target_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    # 人看的对象名，如「订单 SO-20261101-003」「员工张建国」
    target_label: Mapped[str] = mapped_column(String(120), default="")

    # 一句话说清这次做了什么
    summary: Mapped[str] = mapped_column(String(300), default="")

    # JSON 数组：[{"field": "交期", "before": "2026-11-20", "after": "2026-11-25"}]
    # 用 Text 而不是 MySQL JSON 列：逻辑库里也要能跑，且这里不需要在 SQL 里查 JSON 内部
    changes: Mapped[str] = mapped_column(Text, default="")

    ip: Mapped[str] = mapped_column(String(64), default="")
