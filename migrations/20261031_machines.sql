-- 机台主数据补字段 + 机台下移到开工单的订单行。
--
-- 背景：machines 表建库时只有 id/name 两列，work_orders.machine_id 是从未被写入
-- 过的死字段（全库 4 张开工单 machine_id 全 NULL，没有任何一处代码设过它）。
-- 本次把机台改成挂在「订单行」上（work_order_items.machine_id），因为一张开工单
-- 可以同时做几个订单，而人可能在不同机台上做不同的订单；单头存机台无法表达。
-- 单头这列因此删除，不保留「主用机台」冗余 —— 两个数据源迟早漂移。

ALTER TABLE machines
    ADD COLUMN model VARCHAR(50) NOT NULL DEFAULT '' AFTER name,
    ADD COLUMN active TINYINT(1) NOT NULL DEFAULT 1 AFTER model,
    ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER active;

-- 单头的机台列与它的外键一起删。外键不先删，DROP COLUMN 会报 1553。
ALTER TABLE work_orders DROP FOREIGN KEY work_orders_ibfk_1;
ALTER TABLE work_orders DROP COLUMN machine_id;

-- 订单行的机台。可为空 —— 老链路（直接扫订单码开工）不强制关联机台。
ALTER TABLE work_order_items
    ADD COLUMN machine_id VARCHAR(20) NULL AFTER order_id,
    ADD CONSTRAINT fk_work_order_item_machine FOREIGN KEY (machine_id)
        REFERENCES machines (id);

-- 清掉建库时种下的 MC-A01…MC-C02 和一条脏数据 ('1','1')：
-- 它们从未被任何开工单引用过（machine_id 全 NULL），删了零损失；
-- 编号改为系统生成 MC0001，旧的人工编号没有沿用价值。
DELETE FROM machines;
