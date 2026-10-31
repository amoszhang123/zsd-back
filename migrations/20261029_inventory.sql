-- 物料管理（仓库台账）上线：cost_records 增加「纳入库存」三列
--
-- 新表 materials / material_movements / issue_orders / issue_items /
-- scrap_orders / scrap_items 由 Base.metadata.create_all 自动创建（app/models/inventory.py），
-- 本脚本只补 cost_records 上的三列。
--
-- 为什么入库来源只有成本记录：
--   物料是花钱买来的，成本记录已经记了「买了什么、几个、多少钱」。再单独做一张入库单
--   就会和成本记录重复登记同一笔采购，两边的数量迟早会吵。所以「入库」这个动作
--   就是「把一条支出记到某个物料上」，入库流水里存 cost_record_id 反查来源。
--
-- stocked_qty 单独一列而不是复用 quantity：以后若允许一条成本只纳一部分，
-- 库存数量与成本数量就会分叉，提前留出来免得那时改表。
--
-- 三列都可 NULL：NULL = 还没纳入库存。撤销入库后也会被清回 NULL，
-- 那一笔的历史留在 material_movements 里（mtype='revoke'）。
--
-- created_at 不设默认值也没关系：存量记录都是「未纳入」。

ALTER TABLE cost_records
  ADD COLUMN material_id INT NULL AFTER purchase_date,
  ADD COLUMN stocked_qty DECIMAL(12,2) NULL AFTER material_id,
  ADD COLUMN stocked_at DATETIME NULL AFTER stocked_qty,
  ADD INDEX ix_cost_records_material_id (material_id),
  ADD INDEX ix_cost_records_stocked_at (stocked_at);
