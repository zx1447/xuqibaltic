#!/bin/bash
# 实测哪条出口 IP 能过 my.siam-node.cloud 的 Cloudflare 挑战
set -uo pipefail
TARGET="https://my.siam-node.cloud/?p=topup"
XRAY=/tmp/tun/xray
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0'

render_xray() {
  local url="$1" out="$2"
  local uuid host_port host port path
  uuid=$(echo "$url" | sed -E 's|^vless://([^@]+)@.*|\1|')
  host_port=$(echo "$url" | sed -E 's|^vless://[^@]+@([^/?]+).*|\1|')
  host=$(echo "$host_port" | cut -d: -f1)
  port=$(echo "$host_port" | cut -d: -f2); [ -z "$port" ] && port=443
  path=$(echo "$url" | sed -E 's|.*\?([^#]*)#?.*|\1|' | tr '&' '\n' | grep '^path=' | cut -d= -f2- | sed 's|%2F|/|g; s|%3F|?|g')
  [ -z "$path" ] && path="/"
  echo "   uuid=${uuid:0:8}… host=$host port=$port path=$path"
  cat > "$out" <<EOF
{
  "log": {"loglevel": "error"},
  "inbounds": [{"tag":"http","port":10909,"listen":"127.0.0.1","protocol":"http","settings":{}}],
  "outbounds": [{
    "tag":"vless","protocol":"vless",
    "settings":{"vnext":[{"address":"$host","port":$port,"users":[{"id":"$uuid","encryption":"none","flow":""}]}]},
    "streamSettings":{
      "network":"ws","security":"tls",
      "tlsSettings":{"serverName":"$host","fingerprint":"chrome","allowInsecure":false},
      "wsSettings":{"path":"$path","headers":{"Host":"$host"}}
    }
  }]
}
EOF
}

probe() {
  local label="$1" proxy="$2"
  echo ""
  echo "===== [$label] proxy=${proxy:-DIRECT} ====="
  local trace sip
  if [ -n "$proxy" ]; then
    trace=$(curl -s --max-time 20 -x "$proxy" https://www.cloudflare.com/cdn-cgi/trace || true)
  else
    trace=$(curl -s --max-time 20 https://www.cloudflare.com/cdn-cgi/trace || true)
  fi
  sip=$(echo "$trace" | grep '^ip=' | cut -d= -f2)
  echo "  出口 IP : ${sip:-未知}"
  echo "  loc/warp: $(echo "$trace" | grep -E '^(loc|warp)=' | tr '\n' ' ')"
  [ -z "$sip" ] && { echo "  ⛔ 出口不通，跳过"; return; }

  local hdr code
  if [ -n "$proxy" ]; then
    hdr=$(curl -s -D - -o /dev/null --max-time 25 -x "$proxy" -A "$UA" "$TARGET" || true)
  else
    hdr=$(curl -s -D - -o /dev/null --max-time 25 -A "$UA" "$TARGET" || true)
  fi
  code=$(echo "$hdr" | head -1 | awk '{print $2}')
  echo "  Siam HTTP: ${code:-无响应}"
  echo "  cf-mitigated: $(echo "$hdr" | grep -i 'cf-mitigated' | tr -d '\r' || echo '(无)')"
  if [ "$code" = "200" ]; then echo "  ✅ 200 —— 这条出口可以过 CF"; else echo "  ❌ 被拦"; fi
}

# 1. 直连（runner 自己的 Azure IP）
probe "direct" ""

# 2. 固定 VLESS（若 secret 存在）
idx=0
for name in DP_VLESS_URL FENIXHOST_VLESS_URI SCYED_VLESS_URI; do
  url="${!name:-}"
  if [ -z "$url" ]; then echo ""; echo "===== [$name] 未配置，跳过 ====="; continue; fi
  idx=$((idx+1))
  cfg="/tmp/tun/xray-$idx.json"
  echo ""; echo "===== [$name] 准备 xray ====="
  render_xray "$url" "$cfg"
  pkill -f "xray run" 2>/dev/null; sleep 1
  nohup "$XRAY" run -c "$cfg" > "/tmp/tun/xray-$idx.log" 2>&1 &
  sleep 6
  probe "$name" "http://127.0.0.1:10909"
  pkill -f "xray run" 2>/dev/null; sleep 1
done

# 3. Cloudflare WARP（每次注册新身份，出口是 CF 自家 IP）
echo ""; echo "===== [warp] 启动 wgcf + wireproxy ====="
mkdir -p /tmp/warp && cd /tmp/warp
/tmp/tun/wgcf register --accept-tos >/dev/null 2>&1 || echo "  ⚠️ wgcf register 失败"
/tmp/tun/wgcf generate >/dev/null 2>&1 || echo "  ⚠️ wgcf generate 失败"
if [ -f wgcf-profile.conf ]; then
  { echo ""; echo "[Socks5]"; echo "BindAddress = 127.0.0.1:40000"; } >> wgcf-profile.conf
  nohup /tmp/tun/wireproxy -c wgcf-profile.conf > /tmp/warp/wireproxy.log 2>&1 &
  for i in $(seq 1 15); do
    if curl -fsS --max-time 5 -x socks5h://127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace >/dev/null 2>&1; then break; fi
    sleep 1
  done
  probe "warp" "socks5h://127.0.0.1:40000"
  echo ""; echo "--- wireproxy log ---"; tail -5 /tmp/warp/wireproxy.log
else
  echo "  ⛔ WARP 配置未生成"
fi

echo ""; echo "===== 探测结束 ====="
