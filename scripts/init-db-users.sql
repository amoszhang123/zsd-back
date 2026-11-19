-- 创建两个专用数据库账号，去掉 root 的最高权限。
--
-- 为什么要做：应用现在用 root 连库（DB_USER=root）。root 拥有 DROP / GRANT / FILE，
-- 也就是说代码里任何一处 SQL 疏漏，后果不是「数据错了」而是「库没了」。
--
-- ⚠ 执行顺序很重要，别一步砍到底：
--   阶段 A（现在）：建账号，权限先给足（等于 root 的日常能力，但无 DROP/GRANT/FILE）
--   阶段 B（跑够一周后）：逐项收紧，比如去掉 CREATE/ALTER —— 但本项目会自动建表加列，
--                         CREATE/ALTER 是必须保留的，别删。
--
-- 执行方式（在服务器上，用 root）：
--   docker exec -i zsd-db mysql -uroot -p<root密码> < scripts/init-db-users.sql
--   然后把 .env 的 DB_USER / DB_PASSWORD 改成 zsd_app / 它的密码，docker compose up -d
--
-- 密码请先换成随机串再执行：
--   openssl rand -base64 24
-- 注意 shell 特殊字符：MySQL 里出现 @ / 单引号 会麻烦，用纯字母数字最省事。

-- ---------------------------------------------------------------------------
-- 1) 应用账号：日常读写 + 建表加列，但删不了库、删不了表、建不了账号
-- ---------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'zsd_app'@'%' IDENTIFIED BY 'REPLACE_WITH_APP_PASSWORD';

GRANT SELECT, INSERT, UPDATE, DELETE       -- 日常增删改查
    , CREATE, ALTER, INDEX, REFERENCES     -- create_all 建表 / ensure_schema 加列 / 建索引
    ON factory_erp.* TO 'zsd_app'@'%';

-- 刻意不给：DROP（删库删表）、GRANT OPTION（建账号）、FILE（读写服务器文件）、
--          SUPER（改全局变量、杀进程）、PROCESS（看所有连接）

-- ---------------------------------------------------------------------------
-- 2) 备份账号：只读，连删数据的能力都没有
-- ---------------------------------------------------------------------------
CREATE USER IF NOT EXISTS 'zsd_backup'@'%' IDENTIFIED BY 'REPLACE_WITH_BACKUP_PASSWORD';

-- mysqldump 需要：SELECT 读数据、LOCK TABLES（保险，配 --single-transaction 时其实不锁）、
-- SHOW VIEW 导视图、TRIGGER / EVENT 导触发器和事件
GRANT SELECT, LOCK TABLES, SHOW VIEW, TRIGGER, EVENT
    ON factory_erp.* TO 'zsd_backup'@'%';

FLUSH PRIVILEGES;

-- ---------------------------------------------------------------------------
-- 3) 自检：确认权限真的按预期生效（执行完后跑一下，看输出对不对）
-- ---------------------------------------------------------------------------
SELECT user, host FROM mysql.user WHERE user IN ('zsd_app', 'zsd_backup');
SHOW GRANTS FOR 'zsd_app'@'%';
SHOW GRANTS FOR 'zsd_backup'@'%';
