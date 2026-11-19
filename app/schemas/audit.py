from datetime import datetime

from pydantic import BaseModel


class AuditChangeOut(BaseModel):
    field: str
    label: str = ""
    before: str = ""
    after: str = ""


class AuditLogOut(BaseModel):
    id: int
    created_at: datetime
    user_id: str = ""
    user_name: str = ""
    # web / mp：后台操作和小程序操作分开看
    user_kind: str = ""
    action: str = ""
    action_label: str = ""
    module: str = ""
    module_label: str = ""
    target_id: str = ""
    target_label: str = ""
    summary: str = ""
    changes: list[AuditChangeOut] = []
    ip: str = ""


class AuditOptionItem(BaseModel):
    value: str
    label: str


class AuditPersonItem(BaseModel):
    user_id: str
    user_name: str
    user_kind: str = ""


class AuditOptionsOut(BaseModel):
    modules: list[AuditOptionItem] = []
    actions: list[AuditOptionItem] = []
    people: list[AuditPersonItem] = []
    total: int = 0


class AuditLogPageOut(BaseModel):
    total: int = 0
    page: int = 1
    size: int = 50
    items: list[AuditLogOut] = []
