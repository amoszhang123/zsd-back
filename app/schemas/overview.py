from pydantic import BaseModel


class OverviewOut(BaseModel):
    """经营管理三个页面顶部共用的月度概览。

    收入拆成订单收入与其他收入两列返回，前端才知道该在副标题里写什么拆分；
    但 profit 一律按「全部收入 − 全部支出」算 —— 只扣订单口径的话，
    卡片上会出现「收入 − 支出 ≠ 毛利」，看着就像算错了。
    """

    month: str
    order_revenue: float = 0.0
    other_income: float = 0.0
    income: float = 0.0
    cost: float = 0.0
    cash_out: float = 0.0
    expense: float = 0.0
    profit: float = 0.0
