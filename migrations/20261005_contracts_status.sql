-- 合同列表增加合同状态：正常 / 暂停 / 取消 / 结案
--
--   正常  默认值，存量合同一律按「正常」处理
--   暂停  已投产的合同只能暂停、不能取消；暂停可以恢复为正常
--   取消  终态。仅「未投产」的合同可以取消
--   结案  终态。暂停后等结款，再手动结案
--
-- 状态为 暂停 / 取消 / 结案 时，该合同的工单会在接口层被拦住：
-- 不可批量接单（POST /api/orders/batch-confirm）、不可开工（POST /api/production/start-work）。
--
-- 「是否已投产」不落库：由 work_order_items 实时判定（任一关联工单进过开工单即算），
-- 与「合同明细数量锁定」同口径，避免存一份会与生产数据脱节的副本。

ALTER TABLE contracts
  ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT '正常' AFTER purchase_no;

CREATE INDEX ix_contracts_status ON contracts (status);
