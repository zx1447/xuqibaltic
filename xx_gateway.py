#!/usr/bin/env python3
"""xx_gateway.py — 小虾 OpenClaw 网关 WS 驱动（chat.send 让容器内助手跑命令）

协议（逆向自 WorkspaceView chunk，protocol=4）：
  WS  wss://cn.xiaoxia.app/api/instance/agent/ws   Cookie: xx_hosting_token=<JWT>
  帧  {"type":"req","id":"<rand>","method":"connect","params":{minProtocol:4,maxProtocol:4,
       client:{id:"gateway-client",version:"xx-workspace",platform:"web",mode:"backend"},
       role:"operator",scopes:["operator.read","operator.write","operator.admin"],
       caps:["tool-events"],auth:{}}}
  应  {"type":"res","id":..,"ok":true,"payload":{"type":"hello-ok",...}}
  RPC {"type":"req","id":..,"method":"chat.send","params":{"sessionKey","message","idempotencyKey"}}
      {"type":"req","id":..,"method":"chat.history","params":{"sessionKey","limit","maxChars"}}
      {"type":"req","id":..,"method":"sessions.list","params":{}}
      {"type":"req","id":..,"method":"cron.add","params":{...}}
用法：
  python3 xx_gateway.py send  '<消息>' [sessionKey]
  python3 xx_gateway.py history <sessionKey>
  python3 xx_gateway.py sessions
  python3 xx_gateway.py cron-add '<名称>' '<every分钟>' '<agentTurn 消息>'
  python3 xx_gateway.py cron-list
"""
import json, sys, time, uuid, pathlib

STATE = pathlib.Path(__file__).with_name("xiaoxia_state.json")
BASE = "wss://cn.xiaoxia.app"


class Gateway:
    def __init__(self):
        import websocket  # noqa
        tok = json.load(open(STATE))["token"]
        url = f"{BASE}/api/instance/agent/ws"
        self.ws = websocket.create_connection(
            url, timeout=25, cookie=f"xx_hosting_token={tok}",
            header=["User-Agent: Mozilla/5.0", "Origin: https://cn.xiaoxia.app"])
        self._id = 0
        self._req({"type": "req", "id": self._next(), "method": "connect", "params": {
            "minProtocol": 4, "maxProtocol": 4,
            "client": {"id": "gateway-client", "version": "xx-workspace", "platform": "web", "mode": "backend"},
            "role": "operator",
            "scopes": ["operator.read", "operator.write", "operator.admin"],
            "caps": ["tool-events"], "auth": {}}})
        self.hello = self._wait_res(timeout=25)

    def _next(self):
        self._id += 1
        return f"req-{self._id}-{uuid.uuid4().hex[:8]}"

    def _req(self, obj):
        self.ws.send(json.dumps(obj, ensure_ascii=False))

    def rpc(self, method, params=None, timeout=60):
        rid = self._next()
        self._req({"type": "req", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(1, deadline - time.time()))
            try:
                f = json.loads(self.ws.recv())
            except Exception:
                continue
            if f.get("type") == "res" and f.get("id") == rid:
                return f
            # 事件帧忽略
        return {"ok": False, "error": {"message": "rpc timeout"}}

    def _wait_res(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(1, deadline - time.time()))
            try:
                f = json.loads(self.ws.recv())
            except Exception:
                continue
            if f.get("type") == "res":
                return f
            if f.get("type") == "event" and f.get("event") == "connect.challenge":
                pass  # 已在 open 后发 connect，忽略
        return None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    g = Gateway()
    try:
        if not (g.hello and g.hello.get("ok")):
            print("CONNECT FAIL:", json.dumps(g.hello, ensure_ascii=False)[:400])
            sys.exit(1)
        print("[hello-ok]", json.dumps(g.hello.get("payload", {}), ensure_ascii=False)[:300])
        if cmd == "send":
            msg = sys.argv[2]
            key = sys.argv[3] if len(sys.argv) > 3 else f"agent:main:web-{uuid.uuid4().hex[:10]}"
            r = g.rpc("chat.send", {"sessionKey": key, "message": msg,
                                    "idempotencyKey": uuid.uuid4().hex}, timeout=60)
            print("[chat.send]", json.dumps(r, ensure_ascii=False)[:600])
            print("sessionKey =", key)
        elif cmd == "history":
            key = sys.argv[2]
            r = g.rpc("chat.history", {"sessionKey": key, "limit": 40, "maxChars": 200000}, timeout=60)
            print(json.dumps(r, ensure_ascii=False)[:8000])
        elif cmd == "sessions":
            r = g.rpc("sessions.list", {}, timeout=60)
            print(json.dumps(r, ensure_ascii=False)[:8000])
        elif cmd == "cron-add":
            name, mins, message = sys.argv[2], sys.argv[3], sys.argv[4]
            params = {"name": name,
                      "schedule": {"kind": "every", "everyMs": int(mins) * 60000},
                      "sessionTarget": "isolated",
                      "wakeMode": "next-heartbeat",
                      "payload": {"kind": "agentTurn", "message": message}}
            r = g.rpc("cron.add", params, timeout=60)
            print("[cron.add]", json.dumps(r, ensure_ascii=False)[:600])
        elif cmd == "cron-list":
            r = g.rpc("cron.list", {}, timeout=60)
            print(json.dumps(r, ensure_ascii=False)[:8000])
        elif cmd == "cron-run":
            r = g.rpc("cron.run", {"id": sys.argv[2]}, timeout=60)
            print("[cron.run]", json.dumps(r, ensure_ascii=False)[:600])
        else:
            print("unknown cmd", cmd)
    finally:
        g.close()


if __name__ == "__main__":
    main()
