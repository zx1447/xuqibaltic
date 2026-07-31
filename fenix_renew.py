#!/usr/bin/env python3
"""FenixHost service auto-renew via Discord token."""
import json, os, sys, re, html as html_module
import requests
from urllib.parse import urlparse, parse_qs

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
FENIX_SERVICE = os.environ.get("FENIX_SERVICE", "").strip()
FENIX_CLIENT_ID = os.environ.get("FENIX_CLIENT_ID", "1367158139694223486").strip()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"

if not DISCORD_TOKEN or not FENIX_SERVICE:
    print("[fenix] ERROR: missing DISCORD_TOKEN or FENIX_SERVICE")
    sys.exit(1)


def log(msg):
    print(f"[fenix] {msg}", flush=True)


def get_oauth_state(session):
    r = session.get("https://fenixhost.net/oauth/discord", allow_redirects=True, timeout=15)
    if "state=" in r.url:
        return parse_qs(urlparse(r.url).query).get("state", [""])[0]
    return ""


def get_oauth_code(state):
    params = {
        "client_id": FENIX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": "https://fenixhost.net/oauth/discord/callback",
        "scope": "identify email",
        "prompt": "none",
        "state": state,
    }
    r = requests.post("https://discord.com/api/v9/oauth2/authorize", params=params,
        json={"authorize": True, "integration_type": 0},
        headers={"Authorization": DISCORD_TOKEN, "User-Agent": UA, "Content-Type": "application/json"},
        timeout=30)
    if r.status_code != 200:
        log(f"discord authorize HTTP {r.status_code}")
        return ""
    location = r.json().get("location", "")
    if "code=" not in location:
        return ""
    return parse_qs(urlparse(location).query).get("code", [""])[0]


def fenix_login(code, state, session):
    url = f"https://fenixhost.net/oauth/discord/callback?code={code}&state={state}"
    session.get(url, timeout=30, allow_redirects=True)
    return "paymenter_session" in session.cookies


def renew_service(session, service_id):
    url = f"https://fenixhost.net/services/{service_id}"
    r = session.get(url, timeout=30)
    html = r.text
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    if not m:
        return False
    csrf = m.group(1)
    # 找 services.show 组件
    components = re.findall(r'wire:snapshot="([^"]+)"[^>]*wire:id="([^"]+)"', html)
    for snap_encoded, wid in components:
        snap_json = html_module.unescape(snap_encoded)
        try:
            snapshot = json.loads(snap_json)
        except:
            continue
        if snapshot.get("memo", {}).get("name") == "services.show":
            log(f"found services.show: {wid}")
            return call_renew(session, csrf, snap_json, service_id)
    return False


def call_renew(session, csrf, snapshot_json, service_id):
    """POST /paymenter/update with correct Livewire 3 format."""
    headers = {
        "X-CSRF-TOKEN": csrf,
        "X-Livewire": "true",
        "Content-Type": "application/json",
        "Referer": f"https://fenixhost.net/services/{service_id}",
    }
    body = {
        "_token": csrf,
        "components": [{
            "snapshot": snapshot_json,
            "calls": [{"path": "", "method": "renewFree", "params": []}],
        }],
    }
    r = session.post("https://fenixhost.net/paymenter/update", json=body, headers=headers, timeout=30)
    log(f"renew HTTP {r.status_code}, body: {r.text[:300]}")
    if r.status_code == 200:
        if "renewed" in r.text.lower() or "next renewal" in r.text.lower():
            return True
    return False


def main():
    log("=== FenixHost auto-renew start ===")
    session = requests.Session()
    session.headers["User-Agent"] = UA
    state = get_oauth_state(session)
    if not state:
        return 1
    log(f"state: {state[:20]}...")
    code = get_oauth_code(state)
    if not code:
        return 1
    log(f"code: {code[:20]}...")
    if not fenix_login(code, state, session):
        return 1
    log("login OK")
    ok = renew_service(session, FENIX_SERVICE)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
