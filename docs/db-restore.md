# 数据库误删恢复手册

> 出事时照着做，别临场想。**上线前必须演练一次**——第一次用它的时候不该是真正出事那天。

## 先判断是哪一种

| 症状 | 走哪条路 |
|---|---|
| 删了一张/几张单据、改错了金额 | **第二节**：从 binlog 精确回滚（不影响其他数据） |
| 整张表被清空、或者数据大面积乱了 | **第三节**：从备份恢复到临时库再导回 |
| 数据库起不来了、磁盘坏了 | **第四节**：整库恢复（最坏情况） |

所有操作前，**先做一件事**：

```bash
# 立刻再打一份当前备份。你现在看到的状态可能比你以为的更值钱，
# 尤其是准备覆盖恢复之前 —— 覆盖掉就再也没了。
cd /opt/zsd-back && ./scripts/db_backup.sh
```

---

## 一、确定"谁在什么时候删了什么"

审计日志页面：`https://zsd-fab.com/audit`，按「操作=删除」筛。

或者在服务器上直接查：

```bash
docker exec -it zsd-db mysql -uroot -p"$DB_ROOT_PASSWORD" factory_erp -e "
SELECT created_at, user_name, module, target_label, summary
FROM audit_logs
WHERE action='delete'
ORDER BY created_at DESC LIMIT 20;"
```

记下 `created_at`（删除时间）和 `target_label`（删的是哪张单）。

---

## 二、从 binlog 精确回滚（推荐，影响最小）

原理：binlog 里记着 `DELETE` 语句删掉的**完整行数据**（`ROW` 格式下是整行旧值），
把它转成 `INSERT` 就能把那几行放回去，其他数据一点不动。

### 2.1 确认 binlog 开着，找到时间点对应的文件

```bash
docker exec zsd-db mysql -uroot -p"$DB_ROOT_PASSWORD" -e "SHOW VARIABLES LIKE 'log_bin';"
docker exec zsd-db mysql -uroot -p"$DB_ROOT_PASSWORD" -e "SHOW BINARY LOGS;"
```

按删除时间（比如 `2026-11-19 11:47`）选对应的文件名：

```bash
docker exec zsd-db mysqlbinlog \
  --start-datetime="2026-11-19 11:40:00" \
  --stop-datetime="2026-11-19 11:50:00" \
  --base64-output=DECODE-ROWS -v \
  /var/lib/mysql/binlog.000042 > /tmp/del.txt
```

打开 `/tmp/del.txt` 找 `### DELETE FROM` / `### WHERE` 段，确认这就是要恢复的那几行。

### 2.2 生成回滚 SQL 并在事务里先看一遍

binlog 的 `ROW` 格式里，`### SET` 后面跟的就是被删的旧值：

```
### DELETE FROM `factory_erp`.`orders`
### WHERE
###   @1='WO80014AA'
###   @2='OD800014'
###   ...
```

按列顺序拼成 `INSERT`。**别手抄**，用下面的方式从 binlog 自动生成：

```bash
# 把 DELETE 事件转成 INSERT（binlog2sql 需要单独装，比手写安全得多）
# 安装：pip3 install binlog2sql
python3 -m binlog2sql \
  -h 127.0.0.1 -P 3306 -u root -p"$DB_ROOT_PASSWORD" \
  -d factory_erp --start-file='binlog.000042' \
  --start-datetime='2026-11-19 11:40:00' --stop-datetime='2026-11-19 11:50:00' \
  -B > /tmp/rollback.sql
```

`-B` 表示生成回滚语句（DELETE → INSERT），`/tmp/rollback.sql` 里就是可以直接执行的 INSERT。

**执行前先在新起的事务里检查**：

```bash
docker exec -i zsd-db mysql -uroot -p"$DB_ROOT_PASSWORD" factory_erp < /tmp/rollback.sql
# 再去页面确认那条单回来了，数据对不对
```

如果导错了（比如把不该恢复的也恢复了），立刻用审计日志 + 同样的方法反向再删一次；
所以**执行前一定要看一眼 `/tmp/rollback.sql` 的内容**。

---

## 三、从备份恢复到临时库，再把缺的数据导回生产

适用于"整张表被清空"或"binlog 已经被清理（默认只留几天）"。

### 3.1 起一个临时 MySQL，导入备份

```bash
LATEST=$(ls -1t /opt/zsd-back/backups/daily/*.sql.gz | head -1)
echo "使用备份：$LATEST"

docker run -d --name zsd-recover -e MYSQL_ROOT_PASSWORD=temp123 --network none mysql:8.0
# 等它就绪（约 20 秒）
for i in $(seq 1 60); do
  docker exec zsd-recover mysqladmin ping -uroot -ptemp123 --silent 2>/dev/null && break
  sleep 1
done

docker exec zsd-recover mysql -uroot -ptemp123 \
  -e "CREATE DATABASE factory_erp DEFAULT CHARACTER SET utf8mb4"
gunzip -c "$LATEST" | docker exec -i zsd-recover mysql -uroot -ptemp123 factory_erp
```

### 3.2 在临时库里找到缺的数据，导出成 INSERT

```bash
# 例如找回订单
docker exec zsd-recover mysql -uroot -ptemp123 factory_erp -e "
SELECT * FROM orders WHERE id='WO80014AA'\G" | tee /tmp/row.txt
```

确认无误后，用 `mysqldump` 只导这几行（最稳）：

```bash
docker exec zsd-recover mysqldump -uroot -ptemp123 --no-create-info \
  --where="id='WO80014AA'" factory_erp orders > /tmp/one_row.sql
cat /tmp/one_row.sql          # 看一眼
docker exec -i zsd-db mysql -uroot -p"$DB_ROOT_PASSWORD" factory_erp < /tmp/one_row.sql
```

### 3.3 用完删掉临时容器

```bash
docker rm -f zsd-recover
```

---

## 四、整库恢复（最坏情况：库起不来 / 磁盘坏了）

**这会覆盖现有全部数据**，只有确认"现在这份已经完全不可用"才走这条。

```bash
cd /opt/zsd-back

# 1) 先尝试保住现状（哪怕库起不来，文件可能还在）
docker compose stop web
cp -a /var/lib/docker/volumes/zsd-back_mysql_data /root/mysql_data_broken_$(date +%s) 2>/dev/null || true

# 2) 起一个干净的 db 容器（保留 web）
docker compose up -d db
for i in $(seq 1 60); do
  docker exec zsd-db mysqladmin ping -uroot -p"$DB_ROOT_PASSWORD" --silent 2>/dev/null && break
  sleep 1
done

# 3) 导入最新备份
LATEST=$(ls -1t backups/daily/*.sql.gz | head -1)
echo "恢复自：$LATEST"
gunzip -c "$LATEST" | docker exec -i zsd-db mysql -uroot -p"$DB_ROOT_PASSWORD"

# 4) 起应用
docker compose up -d web
curl -s https://zsd-fab.com/health && echo "  ← 服务已恢复"
```

恢复完**立刻检查**：

1. 登录后台，页面能打开、订单列表有数据
2. 小程序能打卡
3. `https://zsd-fab.com/audit` 有数据

---

## 五、如果是从 OSS 拿备份（本机也丢了）

```bash
ossutil cp oss://<bucket>/db-backup/daily/ -r /opt/zsd-back/backups/daily/ --update
ls -lt /opt/zsd-back/backups/daily/ | head
# 之后走第四节
```

---

## 六、恢复后的收尾

1. **记一笔**：恢复到了哪个时间点、丢了哪些数据、原因是什么。写在 `docs/` 下留档。
2. **补数据**：把备份点之后丢失的业务补录进去（这时候审计日志能帮上忙——
   它记着那段时间谁做了什么）。
3. **检查根因**：如果是误删，看要不要收紧那个人的权限；如果是程序 bug，先修 bug 再恢复。

---

## 附：常用命令速查

```bash
# 手动备份一次
/opt/zsd-back/scripts/db_backup.sh

# 看备份状态
ls -lht /opt/zsd-back/backups/daily/ | head
tail -30 /var/log/db-backup.log

# 手动跑一次恢复演练
/opt/zsd-back/scripts/db_restore_drill.sh

# 看 binlog 是否开启、保留多久
docker exec zsd-db mysql -uroot -p"$DB_ROOT_PASSWORD" -e "
SHOW VARIABLES LIKE 'log_bin';
SHOW VARIABLES LIKE 'binlog_expire_logs_seconds';
SHOW BINARY LOGS;"
```

**binlog 保留期**：默认 30 天（`binlog_expire_logs_seconds=2592000`）。如果磁盘紧张，
可以在 `docker-compose.yml` 的 db 命令里加 `--binlog-expire-logs-seconds=604800`（7 天）。
磁盘够就别改——binlog 越长，能挽回的时间窗口越大。
