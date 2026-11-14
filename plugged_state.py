#!/usr/bin/env python3
"""只读探测：打印服务器当前状态，不动手。用于 workflow 的 dry_run。"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("PLUGGED_BASE", "https://panel.plugged.host").rstrip("/")
KEY = os.environ.get("PLUGGED_API_KEY", "").strip()
UUID = os.environ.get("PLUGGED_SERVERS", "").split(",")[0].strip()

if not KEY or not UUID:
    print("❌ 缺 PLUGGED_API_KEY 或 PLUGGED_SERVERS")
    sys.exit(3)

r = urllib.request.Request(
    f"{BASE}/api/client/servers/{UUID}/resources",
    headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json",
             "User-Agent": "Mozilla/5.0"})
d = json.loads(urllib.request.urlopen(r, timeout=30).read())["resources"]
print(f"state: {d['state']} | uptime: {d['uptime']}s | cpu: {d['cpu_absolute']}")
print(f"mem: {d['memory_bytes']} / {d['memory_limit_bytes']} bytes")
