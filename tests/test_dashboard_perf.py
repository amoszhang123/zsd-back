"""员工效能看板的纯计算部分（不依赖数据库）。"""

from datetime import datetime

import pytest

from app.routers.dashboard import MONTH_PATTERN, _good_rate, _month_bounds


def test_month_bounds_is_half_open():
    assert _month_bounds("2026-10") == (
        datetime(2026, 10, 1),
        datetime(2026, 11, 1),
    )


def test_month_bounds_crosses_year():
    assert _month_bounds("2026-12") == (
        datetime(2026, 12, 1),
        datetime(2027, 1, 1),
    )


@pytest.mark.parametrize("month", ["2026-01", "2026-09", "2026-12"])
def test_month_pattern_accepts_valid(month):
    assert MONTH_PATTERN.match(month)


@pytest.mark.parametrize("month", ["2026-00", "2026-13", "26-10", "2026-1", "2026-10-01"])
def test_month_pattern_rejects_invalid(month):
    assert not MONTH_PATTERN.match(month)


def test_good_rate_none_without_output():
    # 没有产出时不能拿 100% 冒充「没数据」
    assert _good_rate(0, 0) is None
    assert _good_rate(0, 3) is None


def test_good_rate_basic():
    assert _good_rate(200, 10) == 95.0
    assert _good_rate(10, 0) == 100.0


def test_good_rate_is_clamped_when_defects_exceed_output():
    # 返工后复检再次判废会把同一件记两次不良，不能出现负良品率
    assert _good_rate(8, 9) == 0.0
