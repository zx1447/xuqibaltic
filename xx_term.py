#!/usr/bin/env python3
"""小虾云电脑终端 WS 驱动（xterm 协议）

协议（2026-11-13 逆向 Terminal-26ucKOuk.js）：
  wss://cn.xiaoxia.app/api/instance/terminal?cols=N&rows=M   （Cookie: xx_hosting_token=<JWT>）
  客户→服务： [0x00]+utf8 = 按键    [0x01]+cols(u16BE)+rows(u16BE) = 尺寸
  服务→客户： [0x00]+bytes = 输出    [0x7e]+text = 状态    [0x7f]+text = 错误

用法：
  python3 xx_term.py 'echo hi'            # 在容器里执行并打印
  python3 xx_term.py --keep 60            # 保持会话 60 秒（调试）
"""
import base64
import json
import os
import sys
import time

import websocket

BASE = os.environ.get("XIAOXIA_BASE", "https://cn.xiaoxia.app").rstrip("/")
EMAIL = os.environ.get("XIAOXIA_EMAIL", "").strip()
PASSWORD = os.environ.get("XIAOXIA_PASSWORD", "").strip()
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xiaoxia_state.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")


def log(*a):
    print(*a, flush=True)


def get_token():
    """取 JWT：优先缓存，其次登录"""
    tok = ""
    try:
        tok = json.load(open(TOKEN_CACHE, encoding="utf-8")).get("token") or ""
    except Exception:  # noqa: BLE001
        pass
    # 验证缓存可用性交给调用方；这里直接尝试登录刷新更省事
    if not tok:
        import urllib.request
        r = urllib.request.Request(
            f"{BASE}/api/auth/login",
            data=json.dumps({"email": EMAIL, "password": PASSWORD}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(r, timeout=20) as resp:
            tok = json.loads(resp.read())["token"]
        try:
            json.dump({"token": tok}, open(TOKEN_CACHE, "w"))
        except Exception:  # noqa: BLE001
            pass
    return tok


class Term:
    def __init__(self, cols=120, rows=40):
        self.token = get_token()
        url = f"{BASE.replace('https', 'wss', 1)}/api/instance/terminal?cols={cols}&rows={rows}"
        self.ws = websocket.create_connection(
            url, timeout=15,
            cookie=f"xx_hosting_token={self.token}",
            header=[f"User-Agent: {UA}", f"Origin: {BASE}"])
        self.ws.send(bytes([1]) + cols.to_bytes(2, "big") + rows.to_bytes(2, "big"))
        self.buf = ""
        self.pump(2.0)  # 等 shell 就绪（吃掉提示符绘制）

    def send(self, text):
        # 终端回车是 \r（\r\n 双发更稳）
        self.ws.send(bytes([0]) + text.replace("\n", "\r\n").encode())

    def pump(self, seconds):
        """收 seconds 秒输出，返回纯文本"""
        out = []
        end = time.time() + seconds
        while time.time() < end:
            try:
                self.ws.settimeout(max(0.2, end - time.time()))
                frame = self.ws.recv()
            except Exception:  # noqa: BLE001
                break
            if not isinstance(frame, (bytes, bytearray)) or len(frame) == 0:
                continue
            t, payload = frame[0], frame[1:]
            if t == 0:
                out.append(payload.decode("utf-8", "replace"))
            elif t == 0x7E:
                log("[状态]", payload.decode("utf-8", "replace"))
            elif t == 0x7F:
                log("[错误]", payload.decode("utf-8", "replace"))
        return "".join(out)

    def run(self, cmd, timeout=120, settle=1.5):
        """执行命令，用 __RC_NNNNN__ 标记收尾，返回 (rc, 去ANSI输出)"""
        import re
        marker = f"__RC_{int(time.time()) % 100000:05d}__"
        self.send(cmd)
        self.pump(settle)  # 吃掉回显
        self.send(f"echo {marker}$?")
        raw = ""
        end = time.time() + timeout
        pat = re.compile(re.escape(marker) + r"(\d+)")
        m = None
        while time.time() < end:
            raw += self.pump(min(5.0, end - time.time()))
            m = pat.search(raw)
            if m:
                break
        rc = int(m.group(1)) if m else -1
        if m:
            raw = raw[:m.start()]
        # 去 ANSI 转义与提示符行
        raw = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\x1b\]", "", raw)
        raw = re.sub(r"\r", "", raw)
        return rc, raw

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def main():
    args = sys.argv[1:]
    t = Term()
    try:
        if args and args[0] == "--keep":
            t.send("\n")
            print(t.pump(float(args[1])))
        else:
            cmd = args[0] if args else "echo hi"
            rc, out = t.run(cmd, timeout=180)
            print(out)
            log(f"[exit {rc}]")
    finally:
        t.close()


if __name__ == "__main__":
    main()
