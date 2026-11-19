"""审计日志：谁在什么时候改了什么东西，改前是什么、改后是什么。

为什么只记写操作：厂里就几个人，把"谁看了哪张单"也记下来，日志会被翻单子的
记录淹没，真正要查的那条反而找不着。写操作才是不可逆、会涉及钱和承诺的。

为什么记录里带"改前改后"而不是只记接口：事后追责时没人看得懂
"PUT /api/orders/3"，但"张三把交期由 11-20 改成 11-25"一眼就明白。

**日志与业务同事务**：record() 只往 session 里 add，不 commit，
跟着业务一起提交或回滚。这样不会出现"操作失败了但日志留着"的假记录。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.audit import AuditLog

# actor 的存放位置：请求级 Session 的 info 字典。
#
# 为什么不用 ContextVar：FastAPI 的同步依赖和同步端点跑在线程池里，
# 两者不一定在同一个线程、也不共享 ContextVar，实测写进去读出来是空的
# （日志里 user_name 全空）。而 get_db() 在一个请求内是同一个 Session 实例，
# 依赖和端点共用它，挂在 Session.info 上一定读得到。
_ACTOR_KEY = "audit_actor"

# 敏感字段：即使出现在 changes 里也要打码，否则审计日志本身成了泄密点
_SENSITIVE_KEYS = {"password", "token", "secret", "webhook", "privkey"}


def set_actor(db: Session, actor: dict | None) -> None:
    try:
        db.info[_ACTOR_KEY] = actor or {}
    except Exception:  # noqa: BLE001 - 记不下来也不能影响业务
        pass


def current_actor(db: Session | None) -> dict:
    if db is None:
        return {}
    try:
        return db.info.get(_ACTOR_KEY) or {}
    except Exception:  # noqa: BLE001
        return {}


def _mask(value):
    if isinstance(value, str) and len(value) > 8:
        return value[:4] + "****"
    return "****"


def clean_changes(changes: list[dict] | None) -> list[dict]:
    """去掉没变化的字段、打码敏感字段。

    没变化的字段会让日志看起来很吵（"改了订单，金额 100→100"），
    而敏感字段（密码哈希、token）不该出现在任何可查询的表里。
    """
    if not changes:
        return []
    cleaned = []
    for item in changes:
        field = str(item.get("field", ""))
        before = item.get("before")
        after = item.get("after")
        if str(before) == str(after):
            continue
        key = field.lower()
        if any(s in key for s in _SENSITIVE_KEYS):
            before, after = _mask(before), _mask(after)
        cleaned.append({
            "field": field,
            "label": item.get("label") or field,
            "before": "" if before is None else str(before),
            "after": "" if after is None else str(after),
        })
    return cleaned


def record(
    db: Session,
    *,
    action: str,
    module: str,
    target_id: str = "",
    target_label: str = "",
    summary: str = "",
    changes: list[dict] | None = None,
    actor: dict | None = None,
) -> AuditLog | None:
    """记一条审计日志（不提交，跟着业务事务走）。

    调用方不需要 try/except：这里面的任何异常都不该让业务失败 ——
    记日志失败最多是丢一条痕迹，不能因此让"改交期"这种正常操作报错。
    """
    try:
        who = actor if actor is not None else current_actor(db)
        row = AuditLog(
            user_id=str(who.get("user_id") or ""),
            user_name=str(who.get("user_name") or ""),
            user_kind=str(who.get("user_kind") or ""),
            action=action,
            module=module,
            target_id=str(target_id or ""),
            target_label=(target_label or "")[:120],
            summary=(summary or "")[:300],
            changes=json.dumps(clean_changes(changes), ensure_ascii=False) if changes else "",
            ip=str(who.get("ip") or ""),
        )
        db.add(row)
        return row
    except Exception:  # noqa: BLE001 - 审计不能阻断业务
        return None


def changes_of(before: dict, after: dict, labels: dict[str, str] | None = None) -> list[dict]:
    """把"改前字典 / 改后字典"转成 changes 结构。

    labels 用来把字段名换成中文（order.due_date → 交期），
    日志是给人看的，别让人对着字段名猜。
    """
    labels = labels or {}
    out = []
    keys = set(before or {}) | set(after or {})
    for key in keys:
        if key not in (before or {}) or key not in (after or {}):
            continue
        out.append({
            "field": key,
            "label": labels.get(key, key),
            "before": (before or {}).get(key),
            "after": (after or {}).get(key),
        })
    return out


def describe(changes: list[dict] | None) -> str:
    """把 changes 压成一句话，让列表页不用展开就能看懂。

    1–3 项逐条写明（交期 11-20 → 11-25；数量 100 → 120），
    再多就只列字段名 —— 十几个字段全铺在列表里没人看，需要细节就点进详情。
    """
    items = clean_changes(changes)
    if not items:
        return ""
    if len(items) <= 3:
        parts = []
        for it in items:
            if it["before"] or it["after"]:
                parts.append(f'{it["label"]} {it["before"] or "空"} → {it["after"] or "空"}')
            else:
                parts.append(it["label"])
        return "；".join(parts)
    names = "、".join(it["label"] for it in items[:5])
    return f"修改 {len(items)} 项：{names}"


def parse_changes(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []
