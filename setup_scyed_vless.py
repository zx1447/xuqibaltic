#!/usr/bin/env python3
"""Parse SCYED_VLESS_URI and write a local Xray SOCKS5 client config."""
import html
import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

raw = html.unescape(os.environ.get("SCYED_VLESS_URI", "").strip())
if not raw:
    sys.exit("SCYED_VLESS_URI is empty")
uri = urlsplit(raw)
if uri.scheme.lower() != "vless" or not uri.username or not uri.hostname:
    sys.exit("Invalid SCYED VLESS URI")

query = {key: values[-1] for key, values in parse_qs(uri.query).items()}
network = query.get("type", "tcp")
security = query.get("security", "none")
server_name = query.get("sni") or uri.hostname
fingerprint = query.get("fp", "chrome")
allow_insecure = query.get("allowInsecure", query.get("insecure", "0")).lower() in {"1", "true", "yes"}

stream = {"network": network, "security": security}
if security == "tls":
    stream["tlsSettings"] = {
        "serverName": server_name,
        "allowInsecure": allow_insecure,
        "fingerprint": fingerprint,
    }
if network == "ws":
    path = unquote(query.get("path", "/"))
    host = query.get("host") or server_name
    stream["wsSettings"] = {
        "path": path,
        "headers": {"Host": host},
    }

config = {
    "log": {"loglevel": "warning"},
    "inbounds": [{
        "listen": "127.0.0.1",
        "port": 1080,
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": False},
    }],
    "outbounds": [{
        "protocol": "vless",
        "settings": {"vnext": [{
            "address": uri.hostname,
            "port": uri.port or 443,
            "users": [{"id": uri.username, "encryption": query.get("encryption", "none")}],
        }]},
        "streamSettings": stream,
    }],
}

output = os.environ.get("XRAY_CONFIG", "/tmp/scyed-xray/config.json")
os.makedirs(os.path.dirname(output), exist_ok=True)
with open(output, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
os.chmod(output, 0o600)
print(f"Xray config ready: {uri.hostname}:{uri.port or 443} ({network}/{security})")
