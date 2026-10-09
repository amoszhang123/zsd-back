-- 员工管理增加「员工属性」：直属员工 / 外包员工
--
-- 与 status、position 一致，用 NOT NULL + 默认值，不给 NULL 留位置：
-- 存量 9 名员工全部按「直属员工」落库（外包是后来才区分的概念，
-- 默认直属不会把已有数据误判成外包）。
--
-- DB 层的 DEFAULT 是这条 ALTER 在**非空表**上能成功的前提 —— 严格模式下
-- 给已有行加 NOT NULL 且无默认值的列会直接报错。加完保留它也无害，
-- 直接写 SQL 插入时漏了这一列也能拿到合理值。
--
-- 取值只有两个，由 app/schemas/employee.py 的 Literal 兜住；
-- 常量见 app/models/production.py 的 EMPLOYEE_TYPES。

ALTER TABLE employees
  ADD COLUMN employee_type VARCHAR(10) NOT NULL DEFAULT '直属员工' AFTER position;
