-- 开工明细表：增加状态流转字段
-- 适用：MySQL 8.0，factory_erp.start_work_records
-- 说明：纯加列，不修改/删除既有数据；已有记录 status 取默认值「开工」
--
--   status        状态，取值「开工」/「完工」
--   completed_at  完工时间，未完工为 NULL
--   completed_qty 完成数量，未完工为 NULL

ALTER TABLE start_work_records
  ADD COLUMN status VARCHAR(10) NOT NULL DEFAULT '开工' AFTER created_at,
  ADD COLUMN completed_at DATETIME NULL AFTER status,
  ADD COLUMN completed_qty INT NULL AFTER completed_at,
  ADD INDEX ix_start_work_records_status (status);
