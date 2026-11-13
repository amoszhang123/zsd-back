from pydantic import BaseModel, Field


class ModuleVisibility(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    visible: bool


class PositionModulesUpdate(BaseModel):
    """整批覆盖某个工种的可见模块。

    前端把注册表里所有模块的开关一次发上来（含 visible=False 的），这样「一行都没有」
    才能明确表示「未配置过」，而「全部关掉」是有行但都不可见。
    """

    modules: list[ModuleVisibility]


class PositionPermissions(BaseModel):
    position: str
    # all_modules=True 表示全部可见。用于尚未配置过的工种 —— 后端不认识前端注册表
    # 里有哪些 key，只能给个标记让前端自己展开成完整清单。
    all_modules: bool
    modules: list[str]
    # 该工种的在册员工数。预置工种即使一个人都没有也会列出，这个数用来解释
    # 「为什么有个没人干的工种」：先配权限，后补员工。
    employee_count: int = 0


class PermissionConfigResponse(BaseModel):
    # 员工在职工种 ∪ 已配置过的工种 ∪ 预置工种（操作工/质检/编程），去掉「管理员」
    # ——管理员恒全可见、不可配，见 routers/permissions.py::_known_positions
    positions: list[PositionPermissions]


class MyPermissionsResponse(BaseModel):
    position: str
    all_modules: bool
    modules: list[str]
