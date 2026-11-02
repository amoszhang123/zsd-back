"""把数据库结构对齐到模型定义（只加表 / 只加列）。

服务启动时 init_db() 会自动跑一次，一般不需要手动执行。手动跑的场景：

    # 预览会执行哪些 DDL，不落库
    docker compose exec -T web python -m app.ensure_schema --dry-run
    # 真的执行
    docker compose exec -T web python -m app.ensure_schema

只做加法：缺的列补上、缺的表由 create_all 建。不改类型、不删列，所以可以随便
重复执行，也不会误伤存量数据。新列一律带类型默认值（0 / '' / CURRENT_TIMESTAMP），
等价于「这条数据属于生产域、机台默认启用」这类合理初始值。
"""

import sys

from app.database import ensure_schema


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    result = ensure_schema(apply=not dry_run)

    head = "将补" if dry_run else "已补"
    if result["added"]:
        print(f"{head} {len(result['added'])} 列:")
        for item in result["added"]:
            print("  +", item)
    else:
        print("表结构已是最新，无需改动")

    if result["failed"]:
        print(f"\n{len(result['failed'])} 列补失败:")
        for item in result["failed"]:
            print("  !", item)

    if result["extra"]:
        print(f"\n库里有、模型里已没有的列（未处理，仅提示）: {len(result['extra'])}")
        for item in result["extra"]:
            print("  ?", item)

    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
