from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ContractItemIn(BaseModel):
    seq: int = 0
    project_no: str = ""
    brand: str = ""
    product: str
    material_no: str = ""
    model: str = ""
    unit: str = ""
    quantity: int = Field(default=0, ge=0)
    unit_price: float = Field(default=0.0, ge=0)
    amount: float = Field(default=0.0, ge=0)
    delivery: str = ""


class ContractImport(BaseModel):
    purchase_no: str = Field(min_length=1)
    party_a: str = ""
    address: str = ""
    bank_name: str = ""
    bank_account: str = ""
    tax_no: str = ""
    phone: str = ""
    signed_at: date | None = None
    total_amount: float = Field(default=0.0, ge=0)
    source_file: str = ""
    items: list[ContractItemIn] = Field(min_length=1)


class ContractItemQuantityUpdate(BaseModel):
    """合同明细只开放数量修改，改完同步到对应工单并重算金额。"""

    quantity: int = Field(gt=0)


class ContractAmountUpdate(BaseModel):
    """合同总金额：要么人工填一个数，要么按明细重算。

    total_amount 与 reset 二选一 —— reset 表示「按明细的含税金额重新汇总」并清掉
    人工标记；只给 total_amount 表示人工填写，之后改数量也不会再覆盖它。
    """

    total_amount: float | None = Field(default=None, ge=0, le=1_000_000_000)
    reset: bool = False


class ContractStatusUpdate(BaseModel):
    """合同状态变更。用 Literal 挡掉非法取值（422），流转规则在路由里判。"""

    status: Literal["正常", "暂停", "取消", "结案"]


class ContractItemOut(BaseModel):
    id: int
    seq: int = 0
    project_no: str = ""
    brand: str = ""
    product: str = ""
    material_no: str = ""
    model: str = ""
    unit: str = ""
    quantity: int = 0
    unit_price: float = 0.0
    amount: float = 0.0
    delivery: str = ""
    order_id: str | None = None
    updated_at: datetime | None = None
    # 非数据库字段：对应工单已进过开工单时为 True，表示数量不可再改
    locked: bool = False
    # 非数据库字段：交期解析后同步到的工单 deadline，解析失败为 None
    order_deadline: date | None = None

    model_config = {"from_attributes": True}


class ContractSummary(BaseModel):
    id: str
    purchase_no: str = ""
    status: str = "正常"
    # 非数据库字段：任一关联工单已到投产（加工）或之后环节即为 True，决定能否取消
    in_production: bool = False
    # 非数据库字段：已接单但还没到终态（出货）的工单数，>0 时不能结案
    unfinished_accepted: int = 0
    party_a: str = ""
    address: str = ""
    bank_name: str = ""
    bank_account: str = ""
    tax_no: str = ""
    phone: str = ""
    signed_at: date | None = None
    total_amount: float = 0.0
    # True 表示总金额由人工填写，改明细数量时不会被自动重算覆盖
    amount_manual: bool = False
    source_file: str = ""
    item_count: int = 0
    total_quantity: int = 0
    created_at: datetime


class ContractDetail(ContractSummary):
    # 交期同步情况：多少条已写入工单 deadline，多少条交期无法识别为日期（脏数据）
    deadline_synced: int = 0
    delivery_unparsed: int = 0
    items: list[ContractItemOut] = []
