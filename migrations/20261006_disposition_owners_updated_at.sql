-- 报废损耗支持修改，用 updated_at 记录最近一次修改时间
--
--   updated_at  NULL 表示建单后从未改过损耗
--
-- 与 contract_items.updated_at 同样的约定，前端据此显示「已修改」标记。

ALTER TABLE disposition_owners
  ADD COLUMN updated_at DATETIME NULL DEFAULT NULL AFTER created_at;
