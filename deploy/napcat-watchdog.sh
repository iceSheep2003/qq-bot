#!/usr/bin/env bash
set -euo pipefail

container="${NAPCAT_CONTAINER:-napcat}"
state_dir="${NAPCAT_WATCHDOG_STATE_DIR:-/var/lib/napcat-watchdog}"
state_file="$state_dir/last-offline-event"
lock_file="$state_dir/lock"

mkdir -p "$state_dir"
exec 9>"$lock_file"
flock -n 9 || exit 0

if ! docker inspect "$container" >/dev/null 2>&1; then
  logger -t napcat-watchdog "container $container does not exist"
  exit 1
fi

running="$(docker inspect "$container" --format '{{.State.Running}}')"
if [[ "$running" != "true" ]]; then
  logger -t napcat-watchdog "container stopped; starting $container"
  docker start "$container" >/dev/null
  exit 0
fi

# A reverse WebSocket can remain healthy while QQ itself is offline. NapCat's
# KickedOffLine/离线 event is therefore the recovery trigger, not container or
# socket state. Only one restart is attempted for each distinct event: an
# invalidated login ticket needs verification and must never cause a restart
# loop that increases account-risk pressure.
offline_event="$(
  docker logs --since 7d "$container" 2>&1 \
    | grep -E 'KickedOffLine|账号状态变更为离线' \
    | tail -n 1 || true
)"
[[ -n "$offline_event" ]] || exit 0

fingerprint="$(printf '%s' "$offline_event" | sha256sum | awk '{print $1}')"
previous="$(cat "$state_file" 2>/dev/null || true)"
[[ "$fingerprint" != "$previous" ]] || exit 0

# Checkpoint before restart so a failed quick login cannot retrigger forever.
printf '%s\n' "$fingerprint" >"$state_file"
logger -t napcat-watchdog "new QQ offline event detected; restarting $container once"
docker restart "$container" >/dev/null
sleep 25

startup="$(docker logs --since 40s "$container" 2>&1 || true)"
if grep -Eq '快速登录错误|请扫描下面的二维码|用户身份已失效' <<<"$startup"; then
  logger -t napcat-watchdog \
    "automatic login failed; saved ticket is invalid and QR/device verification is required"
  exit 2
fi

logger -t napcat-watchdog "restart completed without a quick-login failure"
