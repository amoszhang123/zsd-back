-- web 端「权限管理」模块：按**工种**配置各模块是否可见
--
-- 配置对象是员工管理里的工种（employees.position），不是 web 登录角色：车间关心的是
-- 「操作工/质检/编程能看哪些模块」，同一个登录账号背后可能是不同工种。改造前哪些页面
-- 对谁可见散在前端三处硬编码里（AppLayout 的 adminOnly、router 守卫的 publicPaths、
-- authStore 的 PUBLIC_PATHS），改一处就漏两处；现在统一到这张表，前端启动时拉
-- /api/permissions/my（按 token 里账号绑定的工种）。
--
-- module_key 是前端注册表（erp-mes/src/config/modules.js）里的 key，**不建外键**：
-- 模块清单以前端为准，前端加模块不需要动数据库、也不需要写迁移。工种清单也不存这里，
-- 配置页实时 distinct employees.position。
--
-- 只存管理员显式保存过的工种。某工种一行都没有 = 从没配置过，走后端 DEFAULT_VISIBLE
-- 预置（操作工=生产/质检1/表处/质检2，质检=质检1/质检2，编程=程序/生产），这样上线
-- 不影响现网体验。工种「管理员」恒全可见、不落配置。保存时按工种 delete + insert
-- 整批覆盖，所以「全部关掉」表现为有行但 visible 全为 0。
--
-- 注：这张表是**新建表**，Base.metadata.create_all() 在启动时会自动建；
-- 这里仍留一份 SQL，便于在其它环境手工执行、以及作为变更记录。
-- 早期版本曾按登录角色建过 role_module_permissions，对象选错了，一并清掉。

DROP TABLE IF EXISTS role_module_permissions;

CREATE TABLE IF NOT EXISTS position_module_permissions (
  id          INT         NOT NULL AUTO_INCREMENT,
  position    VARCHAR(20) NOT NULL,
  module_key  VARCHAR(64) NOT NULL,
  visible     TINYINT(1)  NOT NULL DEFAULT 1,
  updated_at  DATETIME    NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_position_module (position, module_key),
  KEY ix_position_module_permissions_position (position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
