#!/usr/bin/env bash
# Deploy Kinna bot from git and restart whatever is already running it.
#
# Written to be pasted into a server shell (Alibaba Cloud Workbench, SSH, or a
# tmux pane). It detects how the bot runs instead of assuming: this repository
# only ships a NapCat watchdog unit, so the bot itself may be a systemd service,
# a container, or a plain process started by hand.
#
#   ./deploy/deploy.sh                 # deploy the branch this clone tracks
#   BRANCH=feat/console-trace-wiki ./deploy/deploy.sh
#   APP_DIR=/opt/qq-bot ./deploy/deploy.sh
#
# It never deletes data, never touches .env, and prints the exact rollback
# command before it changes anything.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${BRANCH:-}"
UNIT_HINT="${UNIT_HINT:-qqbot kinna kinna-bot}"

cd "$APP_DIR"
echo "== 项目目录 $APP_DIR"

if [[ ! -d .git ]]; then
  echo "这里不是 git 仓库，先确认 APP_DIR" >&2
  exit 2
fi

BEFORE="$(git rev-parse --short HEAD)"
echo "== 当前提交 $BEFORE（回滚：git reset --hard $BEFORE && 重启服务）"

# 1. 取代码
TARGET="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
echo "== 拉取 $TARGET"
git fetch --prune origin
if git rev-parse --verify "origin/$TARGET" >/dev/null 2>&1; then
  git checkout "$TARGET"
  git reset --hard "origin/$TARGET"
else
  echo "远端没有 $TARGET，保持在本地分支" >&2
fi
AFTER="$(git rev-parse --short HEAD)"
echo "== 部署到 $AFTER"

# 2. 装依赖（有 venv 就装进 venv，不动系统 python）
if [[ -x .venv/bin/pip ]]; then
  echo "== 安装依赖到 .venv"
  .venv/bin/pip install -q -e . >/dev/null
else
  echo "== 没有 .venv，跳过依赖安装（确认线上用的是哪个解释器）"
fi

# 3. 找出谁在跑
UNIT=""
for candidate in $UNIT_HINT; do
  if systemctl list-unit-files 2>/dev/null | grep -qE "^${candidate}\.service"; then
    UNIT="$candidate"
    break
  fi
done

CONTAINER=""
if [[ -z "$UNIT" ]] && command -v docker >/dev/null 2>&1; then
  CONTAINER="$(docker ps --format '{{.Names}}' | grep -E 'qqbot|kinna' | head -1 || true)"
fi

restart() {
  if [[ -n "$UNIT" ]]; then
    echo "== systemctl restart $UNIT"
    sudo systemctl restart "$UNIT"
  elif [[ -n "$CONTAINER" ]]; then
    echo "== docker restart $CONTAINER"
    docker restart "$CONTAINER" >/dev/null
  else
    echo "== 没找到 systemd unit 或容器。" >&2
    echo "   手动重启后回到这里：确认进程命令里带 qqbot/kinna 再重跑本脚本。" >&2
    return 1
  fi
}

# 4. 健康检查
health() {
  local port token
  port="$(grep -E '^BOT_WEBUI_PORT=' .env 2>/dev/null | cut -d= -f2)"
  token="$(grep -E '^BOT_WEBUI_TOKEN=' .env 2>/dev/null | cut -d= -f2)"
  port="${port:-6200}"
  if [[ -z "$token" ]]; then
    echo "== .env 里没有 BOT_WEBUI_TOKEN，跳过健康检查"
    return 0
  fi
  for _ in $(seq 1 20); do
    if curl -fsS -m 3 -H "Authorization: Bearer $token" "http://127.0.0.1:${port}/api/status" >/dev/null 2>&1; then
      echo "== 健康检查通过（127.0.0.1:${port}）"
      return 0
    fi
    sleep 1
  done
  echo "== 健康检查失败：${port} 上没有响应" >&2
  return 1
}

restart
if ! health; then
  echo "" >&2
  echo "部署失败。回滚：" >&2
  echo "  cd $APP_DIR && git reset --hard $BEFORE && sudo systemctl restart ${UNIT:-<你的服务>}" >&2
  exit 1
fi

echo "== 完成：$BEFORE -> $AFTER"
