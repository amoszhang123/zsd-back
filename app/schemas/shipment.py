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
    # 历史补偿：允许未终检（或终检判定返工/报废/维修）的工单出货。
    # 前端的「历史补偿模式」开关只是把这个人肉动作显式化，最终必须由这里放行，
    # 否则接口仍按老规则一律 400。
    qc_override: bool = False


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
    # 是否在历史补偿模式下绕过终检开出来的（仅内部追溯，不打印在送货单上）
    qc_override: bool = False
    qc_override_note: str = ""
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
    # 出过货的工单数（含只发了一部分的）
    shipped_orders: int = 0
    # 已全量出货、真正完结的工单数，列表里展示的是这个
    fulfilled_orders: int = 0
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
    # 出过货（可能只出了一部分）
    shipped: bool = False
    shipped_quantity: int = 0
    # 累计出货量已够数 → 完结
    fulfilled: bool = False
    # 还能再出多少：合同数量 - 累计已出，最小 0
    remaining_quantity: int = 0
    shipment_doc_no: str = ""
