-- 质检结果分阶段存储：质检1 与质检2 各存一行
-- 原 quality_results.order_id 唯一，两个阶段共用一行，无法区分是谁判定的，
-- 出货完成度要求「以质检2 合格判定数量统计」，因此必须能分开。
--
--   stage  quality1 = 质检1（首检）, quality2 = 质检2（终检）
--
-- 注意语句顺序：order_id 上有外键，MySQL 不允许直接删掉支撑外键的索引
-- （错误 1553），必须先建好以 order_id 打头的复合唯一索引接管外键，再删旧索引。

ALTER TABLE quality_results
  ADD COLUMN stage VARCHAR(20) NOT NULL DEFAULT '' AFTER order_id;

-- 回填历史数据的 stage：只有质检1 的表单会提交合格/不良数，质检2 只提交判定与备注
UPDATE quality_results
   SET stage = CASE WHEN pass_count > 0 OR fail_count > 0 THEN 'quality1' ELSE 'quality2' END;

ALTER TABLE quality_results
  ADD UNIQUE INDEX uq_quality_result_order_stage (order_id, stage);

ALTER TABLE quality_results
  DROP INDEX ix_quality_results_order_id;
