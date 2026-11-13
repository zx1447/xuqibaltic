#!/usr/bin/env bash
# 哪吒监控 agent 保活（固定 UUID 版，2026-11-13 重写）
#
# 设计要点：
#   1. /workspace 是持久卷（xfs），/ 是 overlay 易失 —— 容器重启 /opt/nezha 会没。
#      所以二进制 + 配置常驻 /workspace/nezha/，重启后直接复用，**不重装**。
#   2. UUID 固定为 d1c80647-5c53-5861-12ac-e0ccd02a3049（写死在配置里），
#      重装路径也会强制写回这个 UUID，避免面板出现重复主机。
#   3. 只有持久区和 /opt 都没有二进制时，才走一次安装；装完立刻回填固定配置。
#
# 幂等；由 OpenClaw 定时任务（payload.kind=script）每 5 分钟调用一次。
set -uo pipefail

PERSIST=/workspace/nezha
PBIN="$PERSIST/nezha-agent"
PCFG="$PERSIST/config.yml"
BIN=/opt/nezha/agent/nezha-agent
CFG=/opt/nezha/agent/config.yml

FIXED_UUID="d1c80647-5c53-5861-12ac-e0ccd02a3049"
NZ_SECRET="BFbvpxSlBTUugp3gDzezVKkZ22BV0CeL"
NZ_SERVER="nz.zxydk1715.dpdns.org:443"

alive() { pgrep -f "^$BIN" >/dev/null 2>&1 || pgrep -f "$PBIN" >/dev/null 2>&1; }

write_cfg() {  # $1 = 目标配置文件
  cat > "$1" <<YML
client_secret: $NZ_SECRET
debug: false
disable_auto_update: false
disable_command_execute: false
disable_force_update: false
disable_nat: false
disable_send_query: false
gpu: false
insecure_tls: false
ip_report_period: 1800
report_delay: 3
self_update_period: 0
server: $NZ_SERVER
skip_connection_count: false
skip_procs_count: false
temperature: false
tls: true
use_atomgit_to_upgrade: false
use_gitee_to_upgrade: false
use_ipv6_country_code: false
uuid: $FIXED_UUID
YML
  chmod 600 "$1"
}

start_agent() {
  mkdir -p "$(dirname "$BIN")"
  # 优先用持久区二进制，其次 /opt
  if [ -x "$PBIN" ]; then
    [ -x "$BIN" ] && ! cmp -s "$PBIN" "$BIN" 2>/dev/null && cp -f "$PBIN" "$BIN"
    [ -x "$BIN" ] || cp -f "$PBIN" "$BIN"
  fi
  # 配置：以持久区为准（固定 uuid），持久区没有才用 /opt 的
  if [ -f "$PCFG" ]; then
    cp -f "$PCFG" "$CFG"
  elif [ -f "$CFG" ]; then
    cp -f "$CFG" "$PCFG"
  else
    write_cfg "$PCFG"; cp -f "$PCFG" "$CFG"
  fi
  chmod 600 "$CFG" 2>/dev/null || true

  [ -x /etc/init.d/nezha-agent ] && /etc/init.d/nezha-agent start >/dev/null 2>&1
  sleep 2
  if ! alive; then
    cd /opt/nezha/agent 2>/dev/null || true
    nohup "$BIN" -c "$CFG" >/tmp/nezha_agent.log 2>&1 &
    sleep 3
  fi
}

# ── 1. 已在跑：只保证 UUID 是固定的，然后退出 ──
if alive; then
  cur=$(grep -E '^uuid:' "$CFG" 2>/dev/null | awk '{print $2}')
  if [ "$cur" != "$FIXED_UUID" ]; then
    echo "⚠️ UUID 不符（$cur），改写为固定值并重启"
    write_cfg "$CFG"; cp -f "$CFG" "$PCFG"
    [ -x /etc/init.d/nezha-agent ] && /etc/init.d/nezha-agent restart >/dev/null 2>&1
    pkill -f "^$BIN" 2>/dev/null; sleep 2; start_agent
  fi
  echo "哪吒在线（uuid=$FIXED_UUID）· $(date -u '+%Y-%m-%d %H:%M') UTC"
  exit 0
fi

# ── 2. 没在跑：本地有二进制就直接拉起（不重装） ──
if [ -x "$PBIN" ] || [ -x "$BIN" ]; then
  start_agent
  if alive; then
    echo "哪吒已拉起（本地复用，未重装）· uuid=$FIXED_UUID · $(date -u '+%Y-%m-%d %H:%M') UTC"
    exit 0
  fi
fi

# ── 3. 实在没有二进制（全新容器首次）→ 装一次，然后立刻回填固定 uuid ──
echo "本地无二进制，执行一次安装…"
curl -fsSL https://raw.githubusercontent.com/nezhahq/scripts/main/agent/install.sh -o /tmp/agent.sh \
  && chmod +x /tmp/agent.sh \
  && ( cd /tmp && env NZ_SERVER="$NZ_SERVER" NZ_TLS=true NZ_CLIENT_SECRET="$NZ_SECRET" ./agent.sh ) \
       >/tmp/nezha_reinstall.log 2>&1

# 安装后立刻用固定配置覆盖（installer 会随机生成 uuid）
mkdir -p "$PERSIST"
if [ -x "$BIN" ] && [ ! -x "$PBIN" ]; then
  cp -f "$BIN" "$PBIN"; echo "✅ 已把二进制存入持久区"
fi
write_cfg "$PCFG"; cp -f "$PCFG" "$CFG"

[ -x /etc/init.d/nezha-agent ] && /etc/init.d/nezha-agent restart >/dev/null 2>&1
sleep 2
alive || start_agent

if alive; then
  echo "哪吒已安装并固定 uuid=$FIXED_UUID · $(date -u '+%Y-%m-%d %H:%M') UTC"
else
  echo "FAIL 哪吒未能启动" >&2
  exit 1
fi
