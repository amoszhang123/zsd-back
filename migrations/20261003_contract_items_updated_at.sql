-- 合同明细支持单项修改数量，用 updated_at 记录最近一次修改时间
--
--   updated_at  NULL 表示从未修改过（历史数据、以及导入后未改动的明细）
--
-- 只有数量可改；工单已进过开工单（work_order_items 里有记录）的明细在接口层拒绝修改。

ALTER TABLE contract_items
  ADD COLUMN updated_at DATETIME NULL DEFAULT NULL AFTER order_id;
