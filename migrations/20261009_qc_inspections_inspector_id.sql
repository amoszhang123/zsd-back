-- 质检批次记录「谁检的」
--
-- 原先 qc_inspections 只有 employee_id，而它存的是**第一位负责人**（报废/返工/
-- 维修的损耗归属），不是质检员。web 端 QcJudgmentDialog 里写得很清楚：
--     // employee_id 是历史遗留的单选列（也用于质检批次记录），取第一位负责人
-- 实测印证：现有 4 条记录里 2 条的 employee_id 是张伟（操作工，返工负责人），
-- 另 2 条为 NULL；3 名质检岗员工名下一条都没有。
--
-- 所以新加一列专记质检员，不动 employee_id 的既有语义（改了会把已有的损耗归属
-- 全部解释错）。小程序「我的订单」里「我已质检过的订单」按这一列查。
--
-- 可空：web 端判定目前不传质检员（那边是管理员在操作，没有「当前登录质检员」
-- 的概念），历史记录也补不出来。小程序判定由后端从登录态写入，不接受前端传，
-- 免得能替别人记功。

ALTER TABLE qc_inspections
  ADD COLUMN inspector_id VARCHAR(10) NULL DEFAULT NULL AFTER employee_id;

ALTER TABLE qc_inspections
  ADD INDEX ix_qc_inspections_inspector_id (inspector_id);

ALTER TABLE qc_inspections
  ADD CONSTRAINT qc_inspections_ibfk_inspector
  FOREIGN KEY (inspector_id) REFERENCES employees (id);
