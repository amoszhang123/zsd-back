#!/usr/bin/env bash
# MySQL 全量备份：导出 → 校验完整性 → 本机保留 → 同步 OSS → 异常推微信。
#
# 用法（cron 每 6 小时跑一次）：
#   0 3,9,15,21 * * * /opt/zsd-back/scripts/db_backup.sh >> /var/log/db-backup.log 2>&1
#
# 设计要点（都是踩过的坑，别简化）：
#
# 1. **只导 factory_erp 一个库**，不用 --all-databases。
#    整实例导出在恢复时会把数据库里的一切都覆盖掉，包括你不打算动的库；
#    真出事那天，操作越少越安全。
#
# 2. **--single-transaction**：InnoDB 一致性快照，不锁表。
#    不加这个，备份期间会 Lock tables，车间那边正在提交的单据会卡住。
#
# 3. **必须校验**。mysqldump 失败时退出码非 0，但磁盘写满导致的截断、被 OOM
#    杀掉的 gzip，文件都在、大小也像回事，只有 gzip -t 和结尾标记能识破。
#
# 4. **OSS 的 AK 只给 PutObject**。这样脚本被篡改也删不掉异地副本，
#    勒索病毒加密了本机，OSS 上还留着干净的。清理靠 bucket 生命周期规则，不靠脚本。

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
CONTAINER="${DB_CONTAINER:-zsd-db}"
DB_NAME="${DB_NAME:-$(get_env DB_NAME factory_erp)}"
# 备份专用账号：只有 SELECT / LOCK TABLES / SHOW VIEW，删不掉任何东西
BACKUP_USER="${DB_BACKUP_USER:-$(get_env DB_BACKUP_USER zsd_backup)}"
BACKUP_PASS="${DB_BACKUP_PASSWORD:-$(get_env DB_BACKUP_PASSWORD)}"

# 保留期：日备 30 天，周备 90 天
KEEP_DAYS=30
KEEP_WEEKLY_DAYS=90

# OSS（留空则跳过上传，只做本机备份）
OSS_BUCKET="${OSS_BUCKET:-$(get_env OSS_BUCKET)}"
OSS_PREFIX="${OSS_PREFIX:-db-backup}"

# 微信告警（留空则只写日志）
WEBHOOK="${WEBHOOK:-}"
WEBHOOK_FILE="${WEBHOOK_FILE:-$APP_DIR/.webhook}"   # 也可以把地址放这个文件里，避免写进 crontab
[ -z "$WEBHOOK" ] && [ -f "$WEBHOOK_FILE" ] && WEBHOOK=$(cat "$WEBHOOK_FILE" | tr -d '\n')

notify() {
  local msg="$1"
  echo "[告警] $msg"
  [ -n "$WEBHOOK" ] && curl -s -m 10 -H 'Content-Type: application/json' \
    -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$msg\"}}" "$WEBHOOK" >/dev/null || true
}

fail() {
  notify "❌ 数据库备份失败（$(hostname)）
$1
日志：tail -50 /var/log/db-backup.log"
  exit 1
}

[ -n "$BACKUP_PASS" ] || fail "DB_BACKUP_PASSWORD 为空。请在 .env 或 crontab 里配置 zsd_backup 账号密码"

STAMP=$(date '+%Y%m%d-%H%M')
DAY=$(date '+%Y%m%d')
FILE="$BACKUP_DIR/${DB_NAME}-${STAMP}.sql.gz"
mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"

echo "=== $(date '+%F %T') 开始备份 $DB_NAME ==="

# ---------- 1. 导出 ----------
# 先写到 .tmp，校验通过再改名。避免半截文件被当成有效备份留在目录里。
TMP="$FILE.tmp"
docker exec "$CONTAINER" mysqldump \
  -u"$BACKUP_USER" -p"$BACKUP_PASS" \
  --single-transaction --quick --routines --triggers --events \
  --default-character-set=utf8mb4 --no-tablespaces \
  "$DB_NAME" 2>/tmp/db_backup_err.txt | gzip > "$TMP"
RC=${PIPESTATUS[0]}
if [ "$RC" -ne 0 ]; then
  rm -f "$TMP"
  fail "mysqldump 返回 $RC：$(tail -3 /tmp/db_backup_err.txt | tr '\n' ' ')"
fi

# ---------- 2. 校验 ----------
# 2a. gzip 完整性（能完整解开）
if ! gzip -t "$TMP" 2>/dev/null; then
  rm -f "$TMP"
  fail "备份文件损坏（gzip 校验失败），可能是磁盘写满或被中断"
fi
# 2b. mysqldump 的结束标记：只有正常跑完才会写这行
if ! tail -c 200 "$TMP" | gunzip 2>/dev/null | grep -q "Dump completed"; then
  rm -f "$TMP"
  fail "备份文件缺少 'Dump completed' 标记，导出未正常结束"
fi
# 2c. 体积下限：正常库不可能小于 10KB，太小说明导了个空
SIZE=$(stat -c%s "$TMP" 2>/dev/null || stat -f%z "$TMP")
if [ "$SIZE" -lt 10240 ]; then
  rm -f "$TMP"
  fail "备份文件只有 ${SIZE} 字节，明显不完整"
fi

mv "$TMP" "$FILE"
echo "导出成功：$FILE（$((SIZE/1024)) KB）"

# ---------- 3. 分档保留 ----------
# 每份都进 daily；周日的第一份（凌晨 3 点那次）另存一份到 weekly
cp "$FILE" "$BACKUP_DIR/daily/"
DOW=$(date +%u)
HOUR=$(date +%H)
if [ "$DOW" = "7" ] && [ "$HOUR" = "03" ]; then
  cp "$FILE" "$BACKUP_DIR/weekly/${DB_NAME}-week-${DAY}.sql.gz"
  echo "已存周份：weekly/${DB_NAME}-week-${DAY}.sql.gz"
fi
rm -f "$FILE"   # 根目录不留副本，统一放在 daily/weekly 下

find "$BACKUP_DIR/daily" -name '*.sql.gz' -mtime +$KEEP_DAYS -delete 2>/dev/null
find "$BACKUP_DIR/weekly" -name '*.sql.gz' -mtime +$KEEP_WEEKLY_DAYS -delete 2>/dev/null

DAILY_N=$(ls -1 "$BACKUP_DIR/daily" 2>/dev/null | wc -l | tr -d ' ')
WEEKLY_N=$(ls -1 "$BACKUP_DIR/weekly" 2>/dev/null | wc -l | tr -d ' ')
echo "本机保留：日备 $DAILY_N 份，周备 $WEEKLY_N 份"

# 本机磁盘告警：剩余低于 2GB 就提醒，否则备份失败和磁盘满会一起发生
AVAIL_MB=$(df -Pm "$BACKUP_DIR" | awk 'NR==2 {print $4}')
if [ "${AVAIL_MB:-99999}" -lt 2048 ]; then
  notify "⚠️ 服务器磁盘只剩 ${AVAIL_MB}MB，数据库备份可能很快失败"
fi

# ---------- 4. 同步 OSS ----------
if [ -n "$OSS_BUCKET" ]; then
  if command -v ossutil >/dev/null 2>&1; then
    if ossutil cp "$BACKUP_DIR/daily/$(basename "$FILE")" "oss://$OSS_BUCKET/$OSS_PREFIX/daily/" -f >/dev/null 2>&1; then
      echo "已上传 OSS：$OSS_PREFIX/daily/$(basename "$FILE")"
      # 周份也上传（如果这次生成了）
      [ -f "$BACKUP_DIR/weekly/${DB_NAME}-week-${DAY}.sql.gz" ] && \
        ossutil cp "$BACKUP_DIR/weekly/${DB_NAME}-week-${DAY}.sql.gz" "oss://$OSS_BUCKET/$OSS_PREFIX/weekly/" -f >/dev/null 2>&1
    else
      # 上传失败要告警但不退出：本机备份是有效的，异地这份丢了要尽快补
      notify "⚠️ 数据库备份已落本机，但上传 OSS 失败
文件：$(basename "$FILE")
请检查 ossutil 配置与网络"
    fi
  else
    notify "⚠️ 未安装 ossutil，备份只存在于本机（硬件故障时会一起丢失）"
  fi
else
  echo "未配置 OSS_BUCKET，跳过异地同步"
fi

echo "=== $(date '+%F %T') 备份完成 ==="
