-- 金额/数量列由单精度 FLOAT 改为 DOUBLE
--
-- 原因：SQLAlchemy 的 Float 在 MySQL 上建出来是单精度 FLOAT，只有约 7 位有效数字。
--   实测 198133.87 存进去变成 198134.0；用户录入的 cost_records.unit_price = 1.2
--   被存成 1.2000000476837158。公账流水表 bank_transactions 建表时已直接用
--   Float(53)（DOUBLE），这里把其余列对齐，避免以后再踩。
--
-- 执行前已逐列检测存量损伤（判据：存的值 != ROUND(自己, 2)），只有 2 行受损：
--   cost_records.unit_price = 1.2000000476837158  → 应为 1.2
--   quality_results.rate    = 97.70000076293945   → 应为 97.7
-- 下面第二步的 UPDATE 就是修这两行。这些列都是 2 位小数的业务值，ROUND(x,2) 即为正确值。

ALTER TABLE contract_items
  MODIFY COLUMN unit_price DOUBLE NOT NULL,
  MODIFY COLUMN amount DOUBLE NOT NULL;

ALTER TABLE contracts
  MODIFY COLUMN total_amount DOUBLE NOT NULL;

ALTER TABLE cost_records
  MODIFY COLUMN quantity DOUBLE NOT NULL,
  MODIFY COLUMN unit_price DOUBLE NOT NULL;

ALTER TABLE orders
  MODIFY COLUMN estimated_hours DOUBLE NOT NULL;

-- 该表已退役（质检数据迁到 qc_inspections），但表还在，一并对齐避免日后误读
ALTER TABLE quality_results
  MODIFY COLUMN rate DOUBLE NOT NULL;

ALTER TABLE quote_items
  MODIFY COLUMN price DOUBLE DEFAULT NULL;

UPDATE contract_items SET unit_price = ROUND(unit_price, 2), amount = ROUND(amount, 2);
UPDATE contracts SET total_amount = ROUND(total_amount, 2);
UPDATE cost_records SET quantity = ROUND(quantity, 2), unit_price = ROUND(unit_price, 2);
UPDATE orders SET estimated_hours = ROUND(estimated_hours, 2);
UPDATE quality_results SET rate = ROUND(rate, 2);
UPDATE quote_items SET price = ROUND(price, 2) WHERE price IS NOT NULL;
