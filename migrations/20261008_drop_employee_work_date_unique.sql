-- 放开「一人一天只能有一张开工单」
--
-- 业务规则改为：只要名下没有**未完工**的开工单，就能再开一张，一天开几张都行。
-- 这条规则原先由两处硬保证：
--   1. app/routers/production.py start_work 里的 taken 查询（应用层，已删除）
--   2. work_order_employees 的 UNIQUE(employee_id, work_date)（DB 层，本迁移删除）
-- 只删应用层是不够的 —— DB 约束还在的话，当天第二张会直接撞唯一键抛
-- IntegrityError，接口返回 500 而不是一句能看懂的提示。
--
-- 为什么必须先 ADD INDEX 再 DROP INDEX：
-- uq_employee_work_date 的最左列是 employee_id，MySQL 正拿它当外键
-- work_order_employees_ibfk_2(employee_id → employees.id) 的支撑索引。
-- 直接 DROP 会报 1553 "Cannot drop index ... needed in a foreign key constraint"。
-- 先建一个普通索引顶上，外键改用新索引，才能安全删掉唯一索引。
-- 索引名用 SQLAlchemy 对 index=True 的默认命名，保持模型与库一致。
--
-- work_date 列保留（列表按日期排序/筛选要用），只去掉唯一约束。
-- uq_work_order_employee(work_order_id, employee_id) 不动：同一张开工单里
-- 同一名员工仍然只能出现一次。
--
-- 新的「必须完工结单」规则由应用层保证，见 start_work 的 open_rows 查询。

ALTER TABLE work_order_employees
  ADD INDEX ix_work_order_employees_employee_id (employee_id);

ALTER TABLE work_order_employees DROP INDEX uq_employee_work_date;
