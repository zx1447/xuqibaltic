#!/bin/bash
# 启动 xray + VLESS 代理，转成本地 socks5(10808) + http(10809) 代理
# 用法: VLESS_URL="vless://uuid@host:443?...path=..." ./start-xray.sh
set -e

VLESS_URL="${VLESS_URL:-${CAMOUFOX_VLESS:-}}"
if [ -z "$VLESS_URL" ]; then
    echo "ERROR: VLESS_URL or CAMOUFOX_VLESS env var required"
    echo "  Example: VLESS_URL='vless://uuid@host:443?encryption=none&security=tls&sni=host&fp=chrome&type=ws&path=/foo#tag'"
    exit 1
fi

# 解析 VLESS URL
# vless://UUID@HOST:PORT?encryption=none&security=tls&sni=HOST&fp=chrome&type=ws&path=/PATH#TAG
UUID=$(echo "$VLESS_URL" | sed -E 's|^vless://([^@]+)@.*|\1|')
HOST_PORT=$(echo "$VLESS_URL" | sed -E 's|^vless://[^@]+@([^/?]+).*|\1|')
HOST=$(echo "$HOST_PORT" | cut -d: -f1)
PORT=$(echo "$HOST_PORT" | cut -d: -f2)
[ -z "$PORT" ] && PORT=443
QUERY=$(echo "$VLESS_URL" | sed -E 's|.*\?([^#]*)#?.*|\1|')
PATH_VAL=$(echo "$QUERY" | tr '&' '\n' | grep '^path=' | cut -d= -f2- | sed 's|%2F|/|g; s|%3F|?|g')
[ -z "$PATH_VAL" ] && PATH_VAL="/"

echo "VLESS: uuid=$UUID host=$HOST port=$PORT path=$PATH_VAL"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_TEMPLATE="$SCRIPT_DIR/xray-config.json"
RENDERED_CONFIG="$SCRIPT_DIR/xray-config-rendered.json"

# 渲染 config
sed -e "s|{{VLESS_HOST}}|$HOST|g" \
    -e "s|{{VLESS_UUID}}|$UUID|g" \
    -e "s|{{VLESS_PATH}}|$PATH_VAL|g" \
    "$CONFIG_TEMPLATE" > "$RENDERED_CONFIG"

# 找 xray 二进制
XRAY_BIN=""
for path in "$SCRIPT_DIR/xray" /usr/local/bin/xray /usr/bin/xray "$HOME/bin/xray"; do
    if [ -x "$path" ]; then
        XRAY_BIN="$path"
        break
    fi
done

if [ -z "$XRAY_BIN" ]; then
    echo "xray binary not found, downloading..."
    mkdir -p "$SCRIPT_DIR"
    curl -sL -o /tmp/xray.zip "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"
    cd /tmp && unzip -o xray.zip xray
    chmod +x xray
    mv xray "$SCRIPT_DIR/xray"
    XRAY_BIN="$SCRIPT_DIR/xray"
fi

# 杀掉旧进程
pkill -f "$XRAY_BIN" 2>/dev/null || true
sleep 1

# setsid 启动，脱离 session
setsid "$XRAY_BIN" run -c "$RENDERED_CONFIG" \
    < /dev/null \
    > "$SCRIPT_DIR/xray-stdout.log" \
    2>&1 &

XRAY_PID=$!
echo "xray started PID=$XRAY_PID"
sleep 3

# 验证
if curl -s -x http://127.0.0.1:10809 --max-time 10 https://api.ipify.org; then
    echo ""
    echo "✅ xray proxy working (http://127.0.0.1:10809)"
else
    echo "❌ xray proxy test failed"
    tail -20 "$SCRIPT_DIR/xray-stdout.log"
    exit 1
fi
