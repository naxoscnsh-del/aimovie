#!/usr/bin/env bash
# AI 短片工作台 一键部署
#   bash deploy.sh            首次部署（默认演示模式）/ 更新代码后重新部署
#   bash deploy.sh --prod     首次部署即用正式模式（需随后在 .env 填 Key 和单价）
#   bash deploy.sh --cn       国内服务器：基础镜像、apt、pip 都走国内源（设置会写进 .env，之后更新自动沿用）
#   bash deploy.sh --port 8010 --base /movie   部署到 域名/movie 子路径（只监听本机，由 Nginx 反代）
set -euo pipefail
cd "$(dirname "$0")"

MODE="demo"; CN=0; PORT=""; BASE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prod) MODE="prod" ;;
    --cn) CN=1 ;;
    --port) PORT="$2"; shift ;;
    --base) BASE="/$(echo "$2" | sed 's#^/*##; s#/*$##')"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 1 ;;
  esac
  shift
done

say() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

# ---------- 1. 检查 Docker ----------
say "检查 Docker"
command -v docker >/dev/null || die "没有安装 Docker。Ubuntu/Debian 可执行：curl -fsSL https://get.docker.com | sh"
if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null; then DC="docker-compose"
else die "缺少 docker compose 插件（apt install docker-compose-plugin）"; fi
docker info >/dev/null 2>&1 || die "无法连接 Docker（用 sudo 运行，或把当前用户加入 docker 组）"
echo "使用：$DC"

# ---------- 2. 生成 .env（已存在则保留） ----------
rand() { head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
setkv() {  # setkv KEY VALUE：改写 .env 中的一项（不存在则追加）
  if grep -q "^$1=" .env; then sed -i "s#^$1=.*#$1=$2#" .env; else echo "$1=$2" >> .env; fi
}
NEW_ADMIN_PW=""
if [ ! -f .env ]; then
  say "首次部署：生成 .env"
  cp .env.example .env
  NEW_ADMIN_PW="$(rand 14)"
  setkv ADMIN_USER admin
  setkv ADMIN_PASSWORD "$NEW_ADMIN_PW"
  if [ "$MODE" = "demo" ]; then setkv DEMO_MODE 1; else setkv DEMO_MODE 0; fi
  chmod 600 .env
else
  echo ".env 已存在，保留现有配置"
fi
[ -n "$PORT" ] && setkv APP_PORT "$PORT"
if [ -n "$BASE" ]; then setkv BASE_PATH "$BASE"; setkv APP_BIND 127.0.0.1; fi
APP_PORT="$(grep -E '^APP_PORT=' .env | cut -d= -f2 || true)"; APP_PORT="${APP_PORT:-8000}"
BASE_PATH="$(grep -E '^BASE_PATH=' .env | cut -d= -f2 || true)"

if [ "$CN" = 1 ]; then
  say "国内网络：配置镜像源"
  setkv APT_MIRROR "http://mirrors.aliyun.com"
  setkv PIP_INDEX_URL "https://mirrors.aliyun.com/pypi/simple/"
  # 基础镜像：依次尝试几个国内 Docker Hub 加速入口，用第一个拉得下来的
  CUR_BASE="$(grep -E '^BASE_IMAGE=' .env | cut -d= -f2 || true)"
  if [ -n "$CUR_BASE" ] && docker image inspect "$CUR_BASE" >/dev/null 2>&1; then
    echo "基础镜像已在本机：$CUR_BASE"
  else
    picked=""
    for reg in docker.1ms.run docker.m.daocloud.io docker.1panel.live; do
      img="$reg/library/python:3.12-slim"
      echo "尝试拉取 $img ..."
      if timeout 300 docker pull -q "$img" >/dev/null 2>&1; then picked="$img"; break; fi
    done
    if [ -n "$picked" ]; then
      setkv BASE_IMAGE "$picked"; echo "使用基础镜像：$picked"
    elif docker image inspect python:3.12-slim >/dev/null 2>&1; then
      setkv BASE_IMAGE python:3.12-slim; echo "加速入口都不可用，使用本机已有的 python:3.12-slim"
    else
      die "国内加速入口都拉不到 python:3.12-slim。请在 /etc/docker/daemon.json 配置 registry-mirrors 后重试（见 README）"
    fi
  fi
fi
mkdir -p data backups

# ---------- 3. 构建并启动 ----------
say "构建镜像并启动（首次约 3–10 分钟）"
$DC up -d --build

say "等待服务就绪"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "${ok:-0}" = 1 ] || { $DC logs --tail 50 app; die "服务没有起来，日志如上"; }

# ---------- 4. 自检 ----------
say "系统自检"
$DC exec -T app python -m videogen.doctor || true

# ---------- 5. 结果 ----------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; IP="${IP:-服务器IP}"
DEMO="$(grep -E '^DEMO_MODE=' .env | cut -d= -f2)"
say "部署完成"
if [ -n "$BASE_PATH" ]; then
  echo "  本机地址：http://127.0.0.1:${APP_PORT}${BASE_PATH}/（只监听本机）"
  echo "  对外访问：https://你的域名${BASE_PATH}/ —— 需在 Nginx 加 deploy/nginx-subpath.conf 里的配置"
else
  echo "  访问地址：http://${IP}:${APP_PORT}"
fi
if [ -n "$NEW_ADMIN_PW" ]; then
  echo "  管理员账号：admin"
  echo "  管理员密码：${NEW_ADMIN_PW}    （只显示这一次，也保存在 .env 的 ADMIN_PASSWORD）"
fi
if [ "$DEMO" = "1" ]; then
  echo
  echo "  当前是【演示模式】：不调用接口、不花钱。切到正式模式："
  echo "    1) 编辑 .env：DEMO_MODE=0，填 DEEPSEEK_API_KEY、ARK_API_KEY、PRICE_SEEDREAM_PER_IMAGE、PRICE_SEEDANCE_PER_SECOND"
  echo "    2) 重新运行：bash deploy.sh"
  echo "    3) 自检连通：$DC exec app python -m videogen.doctor --ping"
fi
echo
echo "  常用命令：查看日志 $DC logs -f app ｜ 重启 $DC restart ｜ 停止 $DC down ｜ 备份 bash backup.sh"
echo "  对外网开放请在前面加 HTTPS（见 deploy/Caddyfile.example）"
