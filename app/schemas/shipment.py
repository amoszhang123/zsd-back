from datetime import date, datetime

from pydantic import BaseModel, Field


class ShipmentItemIn(BaseModel):
    order_id: str
    quantity: int = Field(gt=0)
    unit: str = ""
    remark: str = ""


class ShipmentCreate(BaseModel):
    contract_id: str
    address: str = Field(min_length=1)
    items: list[ShipmentItemIn] = Field(min_length=1)


class ShipmentItemOut(BaseModel):
    id: int
    seq: int = 0
    order_id: str | None = None
    material_no: str = ""
    product: str = ""
    model: str = ""
    quantity: int = 0
    unit: str = ""
    remark: str = ""
    # 出货明细本身不存价，单价取自合同明细的含税单价，金额 = 单价 × 本次出货用量
    unit_price: float = 0.0
    amount: float = 0.0

    model_config = {"from_attributes": True}


class ShipmentOut(BaseModel):
    id: int
    doc_no: str
    contract_id: str
    project_no: str = ""
    party_a: str = ""
    address: str = ""
    purchase_no: str = ""
    ship_from: str = ""
    created_at: datetime
    item_count: int = 0
    total_quantity: int = 0
    total_amount: float = 0.0
    items: list[ShipmentItemOut] = []


class ContractProgressOut(BaseModel):
    """出货管理外层：以合同为视角的完成度。"""

    contract_id: str
    purchase_no: str = ""
    party_a: str = ""
    address: str = ""
    signed_at: date | None = None
    project_count: int = 0
    item_count: int = 0
    total_quantity: int = 0
    order_count: int = 0
    qc2_pass_quantity: int = 0
    completion_rate: float = 0.0
    qc2_passed_orders: int = 0
    shipped_orders: int = 0
    shipment_count: int = 0


class ContractOrderOut(BaseModel):
    """合同详情：该合同下每张工单的完成情况。"""

    order_id: str
    project_no: str = ""
    material_no: str = ""
    product: str = ""
    model: str = ""
    unit: str = ""
    contract_quantity: int = 0
    current_step: int = 0
    order_status: str = ""
    qc2_status: str = ""
    qc2_pass_count: int = 0
    counted_quantity: int = 0
    shipped: bool = False
    shipment_doc_no: str = ""
