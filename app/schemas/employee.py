import re
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel

PHONE_PATTERN = re.compile(r"^1[3-9]\d{9}$")


def _validate_phone(value: str | None) -> str | None:
    if value is None:
        return None
    phone = value.strip()
    if phone and not PHONE_PATTERN.fullmatch(phone):
        raise ValueError("手机号格式不正确，应为 11 位中国大陆手机号")
    return phone


Phone = Annotated[str, AfterValidator(_validate_phone)]

# 员工属性只有这两种取值，用 Literal 挡住手滑写进来的脏数据。
# 与 app/models/production.py 的 EMPLOYEE_TYPES 一致（那边是常量，这边是校验）。
EmployeeType = Literal["直属员工", "外包员工"]


class EmployeeResponse(BaseModel):
    id: str
    name: str
    gender: str = "男"
    id_card: str = ""
    phone: str = ""
    hire_date: date | None = None
    status: str = "在职"
    position: str = "操作工"
    employee_type: EmployeeType = "直属员工"

    model_config = {"from_attributes": True}


class EmployeeCreate(BaseModel):
    id: str
    name: str
    gender: str = "男"
    id_card: str = ""
    phone: Phone = ""
    hire_date: date | None = None
    status: str = "在职"
    position: str = "操作工"
    employee_type: EmployeeType = "直属员工"


class EmployeeUpdate(BaseModel):
    name: str | None = None
    gender: str | None = None
    id_card: str | None = None
    phone: Phone | None = None
    hire_date: date | None = None
    status: str | None = None
    position: str | None = None
    employee_type: EmployeeType | None = None
