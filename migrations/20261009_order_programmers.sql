-- 小程序「我的订单」需要知道「谁编程了这张订单」
--
-- 一张订单可能被多名编程员工先后接手（改刀路、补工序、返工重编），所以用关联表
-- 而不是在 orders 上加一个 programmer_id —— 单字段换人接手就会把前一个人抹掉，
-- 「我参与的已编程订单」也就查不全了。
--
-- 写入时机：谁保存过程序时间（orders.machining_minutes），就 upsert 一行。
-- 读取：小程序「我的订单」列表 =
--   所有待编程订单（current_step=2 且 machining_minutes IS NULL）
--   ∪ 本表里有我记录的订单
--
-- (order_id, employee_id) 唯一：同一个人反复保存同一张单只留一行，updated_at 跟着走。
--
-- 注：这张表是**新建表**，Base.metadata.create_all() 在启动时会自动建；
-- 这里仍留一份 SQL，便于在其它环境手工执行、以及作为变更记录。

CREATE TABLE IF NOT EXISTS order_programmers (
  id          INT          NOT NULL AUTO_INCREMENT,
  order_id    VARCHAR(20)  NOT NULL,
  employee_id VARCHAR(20)  NOT NULL,
  created_at  DATETIME     NOT NULL,
  updated_at  DATETIME     NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_order_programmer (order_id, employee_id),
  KEY ix_order_programmers_order_id (order_id),
  KEY ix_order_programmers_employee_id (employee_id),
  CONSTRAINT order_programmers_ibfk_1 FOREIGN KEY (order_id)    REFERENCES orders (id),
  CONSTRAINT order_programmers_ibfk_2 FOREIGN KEY (employee_id) REFERENCES employees (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
