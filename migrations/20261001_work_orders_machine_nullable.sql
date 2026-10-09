-- 开工不再需要机台信息：work_orders.machine_id 由 NOT NULL 改为可空
-- 采用非破坏性方案：保留列与历史值（现有开工单的机台编号仍在库里），
-- 只是新建的开工单不再写入，界面也不再显示。
-- 若确认不再需要历史机台数据，可另行执行：
--   ALTER TABLE work_orders DROP FOREIGN KEY work_orders_ibfk_1;
--   ALTER TABLE work_orders DROP COLUMN machine_id;

ALTER TABLE work_orders
  MODIFY COLUMN machine_id VARCHAR(20) NULL;
