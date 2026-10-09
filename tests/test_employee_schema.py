import pytest
from pydantic import ValidationError

from app.schemas.employee import EmployeeCreate, EmployeeUpdate


def test_create_accepts_valid_phone():
    emp = EmployeeCreate(id="100010", name="李四", phone="13800001010")
    assert emp.phone == "13800001010"


def test_create_strips_phone_whitespace():
    emp = EmployeeCreate(id="100010", name="李四", phone=" 13800001010 ")
    assert emp.phone == "13800001010"


def test_create_allows_empty_phone():
    assert EmployeeCreate(id="100010", name="李四").phone == ""


@pytest.mark.parametrize(
    "phone",
    ["1380000101", "138000010100", "23800001010", "12345678901", "1380000101a", "138-0000-1010"],
)
def test_create_rejects_invalid_phone(phone):
    with pytest.raises(ValidationError) as exc_info:
        EmployeeCreate(id="100010", name="李四", phone=phone)
    assert exc_info.value.errors()[0]["loc"] == ("phone",)


def test_update_accepts_valid_phone():
    assert EmployeeUpdate(phone="15221199464").phone == "15221199464"


def test_update_rejects_invalid_phone():
    with pytest.raises(ValidationError) as exc_info:
        EmployeeUpdate(phone="1522119946")
    assert exc_info.value.errors()[0]["loc"] == ("phone",)


def test_update_leaves_unset_phone_as_none():
    update = EmployeeUpdate(name="王五")
    assert update.phone is None
    assert "phone" not in update.model_dump(exclude_unset=True)
