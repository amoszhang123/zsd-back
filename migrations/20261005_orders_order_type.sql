-- 接单管理增加订单类型：自主 / 全外协 / 半外协
--
--   自主    默认值。接单后进入「投产中」(current_step=3)
--   半外协  接单后进入「投产中」(current_step=3)，部分工序厂外加工，厂内仍需开工
--   全外协  接单后直接进入「质检中」(current_step=4)，不在厂内生产
--
-- 全外协没有开工单，完工数量恒为 0，而质检1 的待检队列原本是按
-- SUM(work_order_items.completed_qty) 驱动的 —— 那样全外协工单永远不会出现在
-- 质检1 队列里。因此 quality1 的上游数量对全外协改取订单全量，
-- 见 app/routers/quality.py 的 _upstream_qty_map。
--
-- 接单后类型不可修改，所以不提供更新接口。

ALTER TABLE orders
  ADD COLUMN order_type VARCHAR(20) NOT NULL DEFAULT '自主' AFTER priority;

CREATE INDEX ix_orders_order_type ON orders (order_type);
