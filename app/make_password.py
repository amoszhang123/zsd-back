"""生成 web 登录密码的哈希，写进 .env 的 WEB_USERS。

用法：
    .venv/bin/python -m app.make_password 你的密码
    # 或容器里
    docker compose exec web python -m app.make_password 你的密码

输出形如 scrypt$<salt>$<hash>，整段粘进 WEB_USERS 即可：

    WEB_USERS=admin:scrypt$...:管理员:admin:管理员

改完 .env 必须 `docker compose up -d`（restart 不重读 env_file），
且改完后所有旧 token 立即失效 —— 所有人需要重新登录一次。
"""

import sys

from app.security import hash_password


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    password = sys.argv[1]
    if len(password) < 6:
        print("密码至少 6 位。厂里共用电脑，别用 123456 这种。")
        return 1
    digest = hash_password(password)
    # .env 是给 docker compose 读的，里面的 $ 会被当成变量替换。
    # scrypt$salt$hash 直接写进去，compose 会把两段 $... 当未定义变量替换成空串，
    # 哈希就退化成一个孤零零的 "scrypt"，登录必然失败，而且只有一行 WARN 提示。
    # 所以这里直接输出 env 格式：每个 $ 写成 $$（compose 会把 $$ 还原成 $）。
    env_digest = digest.replace("$", "$$")
    print("\n把下面整行粘进 .env（注意 $ 已经写成 $$，是给 docker compose 用的格式）：\n")
    print(f"WEB_USERS=admin:{env_digest}:管理员:admin:管理员")
    print("\n多个账号用 ; 分隔，例如：")
    print(
        f"WEB_USERS=admin:{env_digest}:管理员:admin:管理员;"
        "operator:<另一个哈希>:操作员:operator:操作工"
    )
    print(
        "\n改完 .env 必须 `docker compose up -d` 重建容器（restart 不重读 env_file）。\n"
        "自检：docker compose exec web env | grep WEB_USERS 应当显示单个 $ 的哈希。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
