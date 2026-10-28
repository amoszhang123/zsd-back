from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PositionModulePermission(Base):
    """工种 → web 端模块可见性。

    配置对象是员工管理里的工种（employees.position），不是 web 登录角色：同一个
    operator 账号背后可能是操作工也可能是质检，车间关心的是「这个工种能看哪些模块」。

    模块清单以前端注册表（erp-mes/src/config/modules.js）为准，这里只存 module_key
    字符串、不建外键，前端加模块不用动数据库。工种清单也不存这里，配置页实时
    distinct employees.position。

    保存时按工种整批覆盖，所以「该工种有没有行」就等于「有没有被管理员配置过」；
    没配置过时走 routers/permissions.py 的 DEFAULT_VISIBLE 预置。工种「管理员」
    恒全可见、不落配置。
    """

    __tablename__ = "position_module_permissions"
    __table_args__ = (
        UniqueConstraint("position", "module_key", name="uq_position_module"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position: Mapped[str] = mapped_column(String(20), index=True)
    module_key: Mapped[str] = mapped_column(String(64))
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
