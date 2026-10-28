from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.permission import PositionModulePermission
from app.models.production import POSITION_ADMIN, Employee
from app.routers.auth import get_current_user, require_admin
from app.schemas.permission import (
    MyPermissionsResponse,
    PermissionConfigResponse,
    PositionModulesUpdate,
    PositionPermissions,
)

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

# 权限管理页自身只对「管理员」工种开放：读写配置的接口要求 web admin，而 web admin
# 账号绑定的工种就是管理员（恒全可见），其它工种给了也只会得到一个 403 的空菜单项
SELF_MODULE_KEY = "permissions"

# 工种从没被配置过时的预置可见范围，与改造前车间实际在用的页面保持一致。
# 表里没列到的新工种默认什么都看不到，由管理员在权限管理页逐个放开。
DEFAULT_VISIBLE: dict[str, list[str]] = {
    "操作工": ["production", "quality1", "surface", "quality2"],
    "质检": ["quality1", "quality2"],
    "编程": ["program", "production"],
}


def _known_positions(db: Session) -> list[str]:
    rows = db.query(Employee.position).distinct().all()
    return sorted(p for (p,) in rows if p and p != POSITION_ADMIN)


def _load(db: Session, position: str) -> tuple[bool, list[str]]:
    """返回 (是否全部可见, 可见模块 key 列表)。"""
    if position == POSITION_ADMIN:
        # 管理员工种恒全可见，不读配置 —— 否则管理员可能被配得进不了管理页
        return True, []

    rows = (
        db.query(PositionModulePermission)
        .filter(PositionModulePermission.position == position)
        .all()
    )
    if not rows:
        return False, list(DEFAULT_VISIBLE.get(position, []))

    modules = [r.module_key for r in rows if r.visible and r.module_key != SELF_MODULE_KEY]
    return False, modules


@router.get("/my", response_model=MyPermissionsResponse)
def get_my_permissions(
    user: dict = Depends(get_current_user), db: Session = Depends(get_db)
):
    position = user.get("position", "")
    all_modules, modules = _load(db, position)
    return MyPermissionsResponse(
        position=position, all_modules=all_modules, modules=modules
    )


@router.get("/config", response_model=PermissionConfigResponse)
def get_config(user: dict = Depends(require_admin), db: Session = Depends(get_db)):
    positions = []
    for position in _known_positions(db):
        all_modules, modules = _load(db, position)
        positions.append(
            PositionPermissions(
                position=position, all_modules=all_modules, modules=modules
            )
        )
    return PermissionConfigResponse(positions=positions)


@router.put("/positions/{position}", response_model=PositionPermissions)
def update_position_modules(
    position: str,
    data: PositionModulesUpdate,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if position == POSITION_ADMIN:
        raise HTTPException(400, "管理员工种恒全可见，无需配置")
    if position not in _known_positions(db):
        raise HTTPException(404, "工种不存在")

    visible = {m.key: m.visible for m in data.modules}
    visible.pop(SELF_MODULE_KEY, None)

    db.query(PositionModulePermission).filter(
        PositionModulePermission.position == position
    ).delete()
    for key, is_visible in visible.items():
        db.add(PositionModulePermission(position=position, module_key=key, visible=is_visible))
    db.commit()

    return PositionPermissions(
        position=position,
        all_modules=False,
        modules=[k for k, v in visible.items() if v],
    )
