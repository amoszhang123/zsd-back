#!/usr/bin/env bash
# 每月恢复演练：把最新备份真的导进一个临时 MySQL 容器，对比关键表行数。
#
# 这是整套备份方案里唯一能证明「备份真的能恢复」的环节。文件校验只能证明
# 文件没坏，证明不了能导进去（字符集、外键顺序、MySQL 版本差异都可能失败）。
#
# 用法（cron 每月 1 号凌晨 4 点，避开 3 点的备份）：
#   0 4 1 * * /opt/zsd-back/scripts/db_restore_drill.sh >> /var/log/db-restore-drill.log 2>&1
#
# 演练全程不碰生产库：临时容器用随机端口、不挂载任何卷、跑完即删。

set -uo pipefail

APP_DIR="${APP_DIR:-/opt/zsd-back}"

# 从 .env 读单个变量。**不能 source .env** —— 里面的 $$ 是给 docker compose 的转义，
# 在 shell 里会被展开成进程 PID，密码哈希就毁了。这里只做文本提取，不执行。
get_env() {
  local key="$1" default="${2:-}"
  [ -f "$APP_DIR/.env" ] || { echo "$default"; return; }
  local val
  val=$(grep -E "^${key}=" "$APP_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-)
  val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
  [ -n "$val" ] && echo "$val" || echo "$default"
}


BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
DB_NAME="${DB_NAME:-$(get_env DB_NAME factory_erp)}"

# 读生产库行数用哪个账号：
#   优先应用账号（DB_USER/DB_PASSWORD）—— 它必然有 SELECT 且是应用天天在用的，
#   最不容易错。早期版本直接拿 DB_PASSWORD 当 root 密码用，换成专用账号后
#   就永远 Access denied，八张表全变 "?"，看起来像「备份不完整」，实际是连不上。
#   只有 DB_USER=root 时才去用 DB_ROOT_PASSWORD。
SRC_USER="${SRC_USER:-$(get_env DB_USER root)}"
if [ "$SRC_USER" = "root" ]; then
  SRC_PASS="${SRC_PASS:-$(get_env DB_ROOT_PASSWORD "$(get_env DB_PASSWORD)")}"
else
  SRC_PASS="${SRC_PASS:-$(get_env DB_PASSWORD)}"
fi
SRC_CONTAINER="${DB_CONTAINER:-zsd-db}"
TEST_CONTAINER="zsd-restore-drill"
# 复用生产在用的镜像：本地一定已经拉过，避免临时容器因拉镜像失败/太慢而起不来。
# 想换可以用 DRILL_IMAGE 覆盖。
DRILL_IMAGE="${DRILL_IMAGE:-$(get_env DB_IMAGE docker.m.daocloud.io/library/mysql:8.0)}"
DRILL_PASS="drill-only-not-production"

WEBHOOK=""
WEBHOOK_FILE="${WEBHOOK_FILE:-$APP_DIR/.webhook}"
[ -f "$WEBHOOK_FILE" ] && WEBHOOK=$(cat "$WEBHOOK_FILE" | tr -d '\n')

notify() {
  echo "[告警] $1"
  [ -n "$WEBHOOK" ] && curl -s -m 10 -H 'Content-Type: application/json' \
    -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$1\"}}" "$WEBHOOK" >/dev/null || true
}

fail() {
  docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
  notify "❌ 数据库恢复演练失败（$(hostname)）
$1
这代表「备份存在但可能恢复不了」，请尽快人工介入"
  exit 1
}

# 关键表：漏了哪张都说明备份不完整
TABLES=(orders contracts shipments cost_records cash_entries revenue_records employees attendance_records)

echo "=== $(date '+%F %T') 恢复演练开始 ==="
echo "生产库对比账号：$SRC_USER（读 ${DB_NAME} 的行数）"

LATEST=$(ls -1t "$BACKUP_DIR/daily/"*.sql.gz 2>/dev/null | head -1)
[ -n "$LATEST" ] || fail "找不到任何备份文件（$BACKUP_DIR/daily 为空）"
echo "使用备份：$LATEST"

# ---------- 1. 起临时库 ----------
docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$TEST_CONTAINER" \
  -e MYSQL_ROOT_PASSWORD="$DRILL_PASS" \
  --network none \
  "$DRILL_IMAGE" --default-authentication-plugin=mysql_native_password >/dev/null \
  || fail "临时 MySQL 容器启动失败（镜像 $DRILL_IMAGE 不可用）"

# 等它真的能执行 SQL，最多 90 秒。
#
# 不能用 mysqladmin ping 判断：官方镜像启动时会先起一个临时 server 初始化数据目录，
# 那期间 ping 已经能响（返回 0），但 root 密码还没生效、也不能建库 ——
# 紧接着的 CREATE DATABASE 就会失败，报「创建演练库失败」却看不到原因。
READY=0
for i in $(seq 1 90); do
  if docker exec -e MYSQL_PWD="$DRILL_PASS" "$TEST_CONTAINER" \
       mysql -uroot -e "SELECT 1" >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 1
done
[ "$READY" = "1" ] || {
  docker logs "$TEST_CONTAINER" 2>&1 | tail -10
  fail "临时 MySQL 90 秒内没有就绪（上面是容器日志）"
}

# ---------- 2. 导入 ----------
if ! docker exec -e MYSQL_PWD="$DRILL_PASS" "$TEST_CONTAINER" mysql -uroot \
      -e "CREATE DATABASE \`$DB_NAME\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci" \
      >/tmp/drill_create_err.txt 2>&1; then
  fail "创建演练库失败：$(tr '\n' ' ' < /tmp/drill_create_err.txt | tail -c 300)"
fi

if ! gunzip -c "$LATEST" | docker exec -e MYSQL_PWD="$DRILL_PASS" -i "$TEST_CONTAINER" \
      mysql -uroot "$DB_NAME" >/tmp/drill_import_err.txt 2>&1; then
  fail "导入备份失败：$(tail -5 /tmp/drill_import_err.txt | tr '\n' ' ')"
fi
echo "导入完成"

# ---------- 3. 对比行数 ----------
# 允许少量差异：备份之后生产库还在写入，所以演练库只会「少于或等于」生产。
# 差异超过 20% 才算异常，避免正常业务波动误报。
MISMATCH=""
for t in "${TABLES[@]}"; do
  SRC=$(docker exec -e MYSQL_PWD="$SRC_PASS" "$SRC_CONTAINER" mysql -u"$SRC_USER" -N -B \
        -e "SELECT COUNT(*) FROM \`$DB_NAME\`.\`$t\`" 2>/dev/null || echo "?")
  DST=$(docker exec -e MYSQL_PWD="$DRILL_PASS" "$TEST_CONTAINER" mysql -uroot -N -B \
        -e "SELECT COUNT(*) FROM \`$DB_NAME\`.\`$t\`" 2>/dev/null || echo "?")
  if [ "$SRC" = "?" ] || [ "$DST" = "?" ]; then
    MISMATCH="$MISMATCH"$'\n'"  $t: 生产=$SRC 演练=$DST（查询失败）"
    continue
  fi
  echo "  $t: 生产 $SRC / 演练 $DST"
  if [ "$SRC" -gt 0 ]; then
    DIFF=$(( SRC - DST ))
    # 演练库比生产还多 → 不正常；少超过 20% → 不正常
    if [ "$DST" -gt "$SRC" ] || [ "$DIFF" -gt $(( SRC / 5 )) ]; then
      MISMATCH="$MISMATCH"$'\n'"  $t: 生产=$SRC 演练=$DST"
    fi
  fi
done

docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true

if [ -n "$MISMATCH" ]; then
  fail "恢复后行数与生产偏差过大：$MISMATCH"
fi

notify "✅ 数据库恢复演练通过（$(date '+%F %d')）
备份：$(basename "$LATEST")
关键表行数与生产一致"
echo "=== $(date '+%F %T') 演练通过 ==="
