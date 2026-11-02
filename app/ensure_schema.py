"""补齐数据域列（is_test）。

服务启动时 init_db() 会自动调用 ensure_tenant_columns()，一般不需要手动跑。
这个模块的用处是「代码已上线、但不想为补列重启容器」：

    docker compose exec -T web python -m app.ensure_schema

它是幂等的：已经存在的列不会被改动，也不会碰任何数据行（新列默认 0，即生产域）。
"""

import sys

from app.database import ensure_tenant_columns


def main() -> int:
    missing = ensure_tenant_columns()
    if missing:
        print(f"已补 {len(missing)} 张表: {', '.join(missing)}")
    else:
        print("全部表都已有 is_test 列，无需改动")
    return 0


if __name__ == "__main__":
    sys.exit(main())
