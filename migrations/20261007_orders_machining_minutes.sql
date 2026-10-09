-- 程序管理增加「加工时长（分钟）」，预估工时改为由它算出
--
--   machining_minutes  NULL = 尚未填写（与填了 0 区分开）
--
-- 换算公式（预估工时 × 80% = 加工时长，80% 是加工占整个工时的比例）：
--   estimated_hours(小时) = machining_minutes(分钟) ÷ 60 ÷ 0.8 = machining_minutes ÷ 48
-- 见 app/models/order.py 的 hours_from_machining_minutes()。
--
-- estimated_hours 列保留（很多地方在读），但从此只能通过
-- PATCH /api/orders/{id}/machining 由加工时长算出，不再接受手工填写：
-- OrderCreate / OrderUpdate 里的 estimated_hours 字段已移除。
-- 只有处于「程序」阶段(step=2)的工单可以填写。

ALTER TABLE orders
  ADD COLUMN machining_minutes DOUBLE NULL DEFAULT NULL AFTER estimated_hours;
