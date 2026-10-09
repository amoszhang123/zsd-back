#!/bin/bash
#===============================================================
# zsd-back 一键部署脚本
# 用法: ./deploy.sh [分支名]   默认 main
#===============================================================
set -euo pipefail

#----------- 配置区（按需修改）-----------#
APP_DIR="/opt/zsd-back"
BRANCH="${1:-main}"
COMPOSE="docker compose"
HEALTH_URL="http://127.0.0.1:8000/health"
HEALTH_RETRY=12          # 健康检查重试次数
HEALTH_INTERVAL=5        # 每次间隔秒数
LOG_KEEP_DAYS=7          # 部署日志保留天数

#----------- 颜色 -----------#
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[FAIL]${NC}  $*"; }

#----------- 全程错误捕获：失败自动回滚 -----------#
PREV_COMMIT=""
rollback() {
    if [ -n "$PREV_COMMIT" ]; then
        error "部署失败，回滚到上一个版本: $PREV_COMMIT"
        cd "$APP_DIR"
        git reset --hard "$PREV_COMMIT"
        $COMPOSE up -d --build || error "回滚也失败了，请手动检查！"
    fi
}
trap 'rollback' ERR

#----------- 0. 前置检查 -----------#
cd "$APP_DIR"
[ -f docker-compose.yml ] || { error "找不到 docker-compose.yml"; exit 1; }
command -v docker >/dev/null || { error "Docker 未安装"; exit 1; }

# 记录当前版本（用于回滚）
PREV_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "")

echo ""
echo "=================================================="
echo "   zsd-back 自动部署  $(date '+%Y-%m-%d %H:%M:%S')"
echo "   分支: $BRANCH   当前版本: ${PREV_COMMIT:0:8}"
echo "=================================================="

#----------- 1. 更新代码 -----------#
info "1/6 拉取最新代码..."
git fetch --all --prune
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"
NEW_COMMIT=$(git rev-parse HEAD)
if [ "$PREV_COMMIT" = "$NEW_COMMIT" ]; then
    warn "代码无变化 (${NEW_COMMIT:0:8})，仍将继续重建"
else
    ok "代码已更新: ${PREV_COMMIT:0:8} → ${NEW_COMMIT:0:8}"
fi

#----------- 2. 备份数据库 -----------#
info "2/6 备份数据库..."
BACKUP_DIR="$APP_DIR/backups"
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/db_$(date +%Y%m%d_%H%M%S).sql"
if $COMPOSE ps db 2>/dev/null | grep -q "Up"; then
    if $COMPOSE exec -T db sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --all-databases' > "$BACKUP_FILE" 2>/dev/null; then
        ok "数据库已备份: $(basename "$BACKUP_FILE") ($(du -h "$BACKUP_FILE" | cut -f1))"
    else
        warn "数据库备份失败（可能首次部署无数据），继续..."
        rm -f "$BACKUP_FILE"
    fi
else
    warn "数据库容器未运行，跳过备份"
fi
# 只保留最近 10 个备份
ls -t "$BACKUP_DIR"/db_*.sql 2>/dev/null | tail -n +11 | xargs -r rm -f

#----------- 3. 构建镜像 -----------#
info "3/6 构建镜像..."
$COMPOSE build
ok "镜像构建完成"

#----------- 4. 优雅重启服务 -----------#
info "4/6 重启服务（后台常驻）..."
$COMPOSE up -d --remove-orphans
ok "容器已启动"

#----------- 5. 健康检查 -----------#
info "5/6 等待服务就绪..."
HEALTHY=false
for i in $(seq 1 "$HEALTH_RETRY"); do
    if curl -sf --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    echo -n "."
    sleep "$HEALTH_INTERVAL"
done
echo ""

if [ "$HEALTHY" = true ]; then
    ok "健康检查通过: $HEALTH_URL"
else
    error "健康检查失败！最近日志："
    $COMPOSE logs --tail=30 web
    exit 1        # 触发 trap 回滚
fi

#----------- 6. 清理旧镜像与缓存 -----------#
info "6/6 清理旧镜像和缓存..."
# 只清理悬空镜像和构建缓存，不碰正在用的
docker image prune -f >/dev/null 2>&1 || true
docker builder prune -f --filter "until=24h" >/dev/null 2>&1 || true
docker system df | head -5
ok "清理完成"

#----------- 记录部署日志 -----------#
DEPLOY_LOG="$APP_DIR/deploy.log"
echo "$(date '+%Y-%m-%d %H:%M:%S') 部署成功 commit=${NEW_COMMIT:0:8} branch=$BRANCH" >> "$DEPLOY_LOG"
# 日志轮转
find "$APP_DIR" -maxdepth 1 -name "deploy.log" -mtime +$LOG_KEEP_DAYS -exec truncate -s 0 {} \; 2>/dev/null || true

#----------- 完成 -----------#
echo ""
echo "=================================================="
ok "部署成功！"
echo "   版本:   ${NEW_COMMIT:0:8}"
echo "   时间:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "   服务:   $HEALTH_URL"
echo "   状态:   $($COMPOSE ps --format '{{.Name}} ({{.Status}})' | tr '\n' ' ')"
echo "=================================================="
echo ""
echo "常用命令："
echo "  查看日志:  docker compose logs -f web"
echo "  服务状态:  docker compose ps"
echo "  停止服务:  docker compose down"
