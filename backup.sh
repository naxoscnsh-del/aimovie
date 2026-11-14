#!/usr/bin/env bash
# 备份数据（数据库 + 素材 + 成片）到 backups/，默认保留最近 7 份
#   bash backup.sh
#   bash backup.sh --keep 14
# 定时备份（每天 3 点）：crontab -e 加一行
#   0 3 * * * cd /opt/aimovie && bash backup.sh >> backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
KEEP=7
[ "${1:-}" = "--keep" ] && KEEP="$2"
mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"

# 用 SQLite 在线备份接口拷一份一致的数据库，再打包整个 data 目录
if [ -f data/app.db ]; then
  if docker compose ps --status running app >/dev/null 2>&1 && docker compose ps --status running app | grep -q app; then
    docker compose exec -T app python -c "import sqlite3;s=sqlite3.connect('/app/data/app.db');d=sqlite3.connect('/app/data/app.db.backup');s.backup(d);d.close()"
  else
    python3 -c "import sqlite3;s=sqlite3.connect('data/app.db');d=sqlite3.connect('data/app.db.backup');s.backup(d);d.close()"
  fi
fi
tar czf "backups/aimovie-data-${STAMP}.tgz" --exclude='data/app.db' --exclude='data/app.db-*' data .env
rm -f data/app.db.backup
echo "已备份：backups/aimovie-data-${STAMP}.tgz（$(du -h "backups/aimovie-data-${STAMP}.tgz" | cut -f1)）"
ls -1t backups/aimovie-data-*.tgz | tail -n +$((KEEP + 1)) | xargs -r rm -f
# 恢复：bash deploy.sh 前，把包解压到本目录（tar xzf backups/xxx.tgz），再 mv data/app.db.backup data/app.db
