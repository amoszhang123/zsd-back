from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


from app.schemas.order import (  # noqa: E402
    StepSchema,
    MaterialSchema,
    QualityResultSchema,
    OrderResponse,
    OrderCreate,
    OrderUpdate,
    OrderImportRow,
    DashboardStats,
    DashboardAlert,
)
from app.schemas.production import StartWorkRequest, StartWorkResponse  # noqa: E402
from app.schemas.employee import EmployeeResponse, EmployeeCreate, EmployeeUpdate  # noqa: E402
