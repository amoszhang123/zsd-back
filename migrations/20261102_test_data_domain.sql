-- 测试数据域：给「小程序有入口」的 20 张业务表加 is_test 标记。
--
-- 背景：小程序提审需要一个可以用固定验证码登录的测试账号，且它与真实生产数据
-- 双向不可见。采用「单库 + is_test 标记 + 全局 ORM 过滤」：业务代码的读写路径
-- 一行都不用改，过滤由 app/database.py 里的 Session 事件统一施加。
--
-- 哪些表加：小程序（工作台/我的订单/工单/质检/编程/报价/合同）能读到的业务表。
-- 哪些表不加：物料、成本、出货、银行、权限配置 —— 小程序没有入口，测试账号
-- 也永远读不到，加了只是白白多一列。
--
-- 未加 is_test 的表不做隔离；若将来小程序开放了这些模块的入口，必须同步加列，
-- 否则会串数据。

ALTER TABLE employees            ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE machines             ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE orders               ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE order_steps          ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE order_materials      ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE order_programmers    ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE order_contributions  ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE work_orders          ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE work_order_items     ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE work_order_employees ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE qc_inspections       ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE quality_results      ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE scrap_orders         ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE rework_orders        ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE repair_orders        ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE disposition_owners   ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE quotes               ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE quote_items          ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE contracts            ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE contract_items       ADD COLUMN is_test TINYINT(1) NOT NULL DEFAULT 0;
