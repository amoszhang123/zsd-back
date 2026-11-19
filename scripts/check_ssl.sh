#!/usr/bin/env bash
# SSL 体检：查清 HTTPS 由谁提供、证书在哪、自动续期有没有在跑。
#
# 用法（在服务器上，需要 sudo 才能读 nginx 配置和系统定时任务）：
#     sudo bash scripts/check_ssl.sh
#     sudo bash scripts/check_ssl.sh zsd-fab.com    # 指定域名
#
# 只读脚本：不改任何配置、不申请证书、不重启服务。
# 唯一的"写"是 certbot renew --dry-run 会在 /var/log/letsencrypt 留日志。

DOMAIN="${1:-zsd-fab.com}"
line() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$1"; }

line "1. 谁在监听 80 / 443"
if command -v ss >/dev/null 2>&1; then
  ss -lntp 2>/dev/null | grep -E ':(80|443)\b' || warn "没看到 80/443 的监听（可能被 SELinux 隐藏，试 netstat）"
else
  netstat -lntp 2>/dev/null | grep -E ':(80|443)\b'
fi

line "2. Web 服务器类型与二进制位置"
NGINX_BIN=""
for cand in /www/server/nginx/sbin/nginx /usr/sbin/nginx /usr/local/nginx/sbin/nginx /usr/local/openresty/nginx/sbin/nginx; do
  [ -x "$cand" ] && NGINX_BIN="$cand" && break
done
if [ -n "$NGINX_BIN" ]; then
  ok "找到 $NGINX_BIN"
  "$NGINX_BIN" -v 2>&1 | sed 's/^/  /'
else
  warn "没找到 nginx/openresty 二进制，可能跑在容器里（见第 3 步）"
fi
if [ -d /www/server/panel ]; then
  ok "检测到宝塔面板（/www/server/panel）"
else
  printf '  未检测到宝塔面板\n'
fi

line "3. 反向代理配置里 $DOMAIN 的段落"
if [ -n "$NGINX_BIN" ]; then
  CONF=$("$NGINX_BIN" -T 2>/dev/null | grep -n "server_name.*$DOMAIN" | head -5)
  if [ -n "$CONF" ]; then
    echo "$CONF"
  else
    warn "nginx 配置里没找到 $DOMAIN —— 可能证书和反代在 CDN/负载均衡上，或配置在容器内"
  fi
fi
printf '\n  容器里是否有 nginx：\n'
docker ps --format '  {{.Names}}\t{{.Image}}\t{{.Ports}}' 2>/dev/null | grep -iE 'nginx|openresty|front|web' || printf '  未发现相关容器\n'

line "4. 证书文件位置与内容"
CERT_PATHS=""
if [ -n "$NGINX_BIN" ]; then
  CERT_PATHS=$("$NGINX_BIN" -T 2>/dev/null | grep -hE '^\s*ssl_certificate\s' | awk '{print $2}' | tr -d ';' | sort -u)
fi
# 宝塔的默认位置
for p in /www/server/panel/vhost/cert/$DOMAIN/fullchain.pem /etc/letsencrypt/live/$DOMAIN/fullchain.pem; do
  [ -f "$p" ] && CERT_PATHS="$CERT_PATHS"$'\n'"$p"
done
CERT_PATHS=$(echo "$CERT_PATHS" | grep -v '^$' | sort -u)
if [ -z "$CERT_PATHS" ]; then
  warn "没找到证书文件，翻一下 /etc/letsencrypt/live/ 和 /www/server/panel/vhost/cert/"
else
  while IFS= read -r cert; do
    [ -f "$cert" ] || continue
    printf '  %s\n' "$cert"
    openssl x509 -in "$cert" -noout -subject -issuer -enddate 2>/dev/null | sed 's/^/    /'
    END=$(openssl x509 -in "$cert" -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -n "$END" ]; then
      DAYS=$(( ( $(date -d "$END" +%s 2>/dev/null || date -j -f "%b %d %T %Y %Z" "$END" +%s 2>/dev/null) - $(date +%s) ) / 86400 ))
      if [ "$DAYS" -lt 15 ] 2>/dev/null; then
        warn "只剩 $DAYS 天，续期可能没在跑！"
      else
        ok "剩余 $DAYS 天"
      fi
    fi
    printf '    最近修改时间: %s\n' "$(date -r "$cert" '+%Y-%m-%d %H:%M' 2>/dev/null || stat -c %y "$cert" 2>/dev/null)"
  done <<< "$CERT_PATHS"
fi

line "5. 自动续期任务"
printf '  -- root 的 crontab --\n'
crontab -l 2>/dev/null | grep -iE 'certbot|acme|renew|ssl' || printf '  无\n'
printf '  -- /etc/cron.d 与 cron.daily --\n'
grep -rilE 'certbot|acme|renew' /etc/cron.d /etc/cron.daily 2>/dev/null || printf '  无\n'
printf '  -- systemd timer --\n'
systemctl list-timers --all 2>/dev/null | grep -iE 'certbot|acme' || printf '  无\n'
printf '  -- 宝塔计划任务（脚本文件） --\n'
ls -1 /www/server/cron/ 2>/dev/null | head -10 || printf '  无\n'
printf '  -- 宝塔数据库里的任务（含执行日志路径） --\n'
if [ -f /www/server/panel/data/default.db ]; then
  sqlite3 /www/server/panel/data/default.db \
    "select id,name,type,where1,echo,status from crontab where name like '%证书%' or name like '%ssl%' or type='day' or type='minute-n' limit 10;" 2>/dev/null \
    || printf '  （sqlite3 未安装，跳过；请在面板「计划任务」里看）\n'
fi

line "6. 最近一次续期记录"
for log in /var/log/letsencrypt/letsencrypt.log /www/server/panel/logs/letsencrypt.log /root/.acme.sh/acme.sh.log; do
  if [ -f "$log" ]; then
    printf '  %s（最后修改 %s）\n' "$log" "$(date -r "$log" '+%Y-%m-%d %H:%M' 2>/dev/null || stat -c %y "$log" 2>/dev/null)"
    grep -iE "success|renew|error|fail" "$log" 2>/dev/null | tail -5 | sed 's/^/    /'
  fi
done
if [ -d /root/.acme.sh ]; then
  printf '  -- acme.sh 管理的证书 --\n'
  /root/.acme.sh/acme.sh --list 2>/dev/null | sed 's/^/    /'
fi

line "7. 续期可行性演练（不会真的续，安全）"
if command -v certbot >/dev/null 2>&1; then
  printf '  正在执行 certbot renew --dry-run ...\n'
  certbot renew --dry-run 2>&1 | tail -15 | sed 's/^/    /'
else
  printf '  未安装 certbot（若用宝塔/acme.sh 则正常）\n'
fi

line "结论怎么看"
cat <<'TIP'
  * 第 4 步剩余天数 < 30 且第 5 步没有任何续期任务  →  自动续期没配，到期会全站打不开
  * 第 5 步有任务但第 6 步日志很久没更新          →  任务在跑但失败了（常见：80 端口被占、验证文件 404）
  * 第 7 步 dry-run 出现 "Congratulations" 或 "simulated renewal succeeded"  →  续期链路是通的
  * 证书是 90 天有效期（Let's Encrypt），剩 51 天属于正常，真正的考验在到期前 30 天
TIP
