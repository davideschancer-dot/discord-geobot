#!/bin/bash
# -------------------------------------------------------
# EC2 GEO Redirect Checker — setup script
# -------------------------------------------------------
# Provisions an Amazon Linux 2023 instance to host a tiny
# HTTP API that checks which mirror chancer.bet redirects to
# for a given country, via NordVPN OpenVPN tunnels.
#
# Usage:
#   Set NORDVPN_SERVICE_USER + NORDVPN_SERVICE_PASS + API_KEY
#   in the environment before running, then:
#       sudo -E bash ec2_setup.sh
#
# Runs on: Amazon Linux 2023, x86_64
# Exposes: port 8080, endpoints /check, /health
# -------------------------------------------------------
set -euo pipefail

: "${NORDVPN_SERVICE_USER:?NORDVPN_SERVICE_USER must be set in env}"
: "${NORDVPN_SERVICE_PASS:?NORDVPN_SERVICE_PASS must be set in env}"
: "${API_KEY:?API_KEY must be set in env (shared secret for /check)}"

echo "=== Installing packages ==="
dnf install -y openvpn python3 python3-pip

echo "=== Installing Python deps ==="
pip3 install flask requests

echo "=== Writing NordVPN credentials ==="
mkdir -p /etc/openvpn/client
cat > /etc/openvpn/client/credentials.txt <<EOF
${NORDVPN_SERVICE_USER}
${NORDVPN_SERVICE_PASS}
EOF
chmod 600 /etc/openvpn/client/credentials.txt

echo "=== Deploying redirect checker API ==="
cat > /opt/redirect_checker.py <<'PYEOF'
"""
GEO Redirect Checker API
Spins up a NordVPN OpenVPN tunnel for the requested country,
checks chancer.bet redirect, tears down the tunnel.
"""
import json
import os
import subprocess
import threading
import time
import urllib.parse

import requests as req
import urllib3
from flask import Flask, jsonify, request

urllib3.disable_warnings()
app = Flask(__name__)

TARGET = "chancer.bet"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
API_KEY = os.environ.get("REDIRECT_API_KEY", "change-me")
CREDS_FILE = "/etc/openvpn/client/credentials.txt"
OVPN_BASE = "https://downloads.nordcdn.com/configs/files/ovpn_udp/servers"

# NordVPN country IDs, used to ask the recommendations API for a
# currently-online server. Get more via https://api.nordvpn.com/v1/countries
COUNTRY_ID = {
    "hu": 98,
    "gr": 84,
    "pl": 174,
    "dk": 58,
    "fr": 74,
    "ae": 226,
    "no": 163,
}

# Fallback server numbers, used only if the recommendations API is
# unreachable. NordVPN retires/renumbers servers over time, so these
# WILL go stale — the API lookup above is the primary path.
SERVER_MAP = {
    "hu": "hu69",
    "gr": "gr69",
    "pl": "pl231",
    "dk": "dk150",
    "fr": "fr550",
    "ae": "ae69",
    "no": "no200",
}

vpn_lock = threading.Lock()


def _host(url):
    return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")


def resolve_server(country_code):
    """Return the current recommended NordVPN hostname (e.g. 'pl231.nordvpn.com')
    for a country. Tries the live recommendations API first, then falls back to
    the hardcoded SERVER_MAP. Returns None if neither yields a server."""
    cid = COUNTRY_ID.get(country_code)
    if cid:
        try:
            resp = req.get(
                "https://api.nordvpn.com/v1/servers/recommendations",
                params={"filters[country_id]": cid, "limit": 1},
                timeout=10,
            )
            data = resp.json()
            if data and data[0].get("hostname"):
                return data[0]["hostname"]
        except Exception:
            pass  # fall through to the hardcoded fallback

    server = SERVER_MAP.get(country_code)
    if server:
        return f"{server}.nordvpn.com"
    return None


def download_ovpn(hostname):
    """Download the NordVPN .ovpn config for a specific server hostname if not
    cached. Cached per-server so a retired server's config is never reused for
    a different one. Returns the local path, or None on failure."""
    server = hostname.split(".")[0]
    path = f"/etc/openvpn/client/{server}.ovpn"
    if os.path.exists(path):
        with open(path) as f:
            first_line = f.readline()
        if first_line.startswith("<") or "404" in first_line:
            os.remove(path)
        else:
            return path

    url = f"{OVPN_BASE}/{hostname}.udp.ovpn"
    result = subprocess.run(
        ["curl", "-so", path, "-w", "%{http_code}", url],
        capture_output=True, text=True,
    )
    if result.stdout.strip() != "200":
        if os.path.exists(path):
            os.remove(path)
        return None

    with open(path) as f:
        content = f.read()
    content = content.replace("auth-user-pass", f"auth-user-pass {CREDS_FILE}")
    content += "\nroute-nopull\nscript-security 2\n"
    with open(path, "w") as f:
        f.write(content)
    return path


def start_vpn(country_code):
    """Start OpenVPN tunnel for the given country.
    Returns (True, None) on success, or (False, reason) on failure so callers
    can tell apart 'no server', 'config download failed', and 'tunnel timeout'."""
    subprocess.run(["killall", "openvpn"], capture_output=True)
    time.sleep(1)

    hostname = resolve_server(country_code)
    if not hostname:
        return False, f"no server known for '{country_code}'"

    ovpn_path = download_ovpn(hostname)
    if not ovpn_path:
        return False, f"config download failed for {hostname}"

    subprocess.Popen(
        ["openvpn", "--config", ovpn_path, "--dev", "tun1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    for _ in range(20):
        time.sleep(1)
        result = subprocess.run(["ip", "link", "show", "tun1"], capture_output=True)
        if result.returncode == 0:
            # Route chancer.bet's Cloudflare IP range through the VPN tunnel.
            subprocess.run(["ip", "route", "del", "104.24.14.0/24", "dev", "tun1"], capture_output=True)
            subprocess.run(["ip", "route", "del", "104.24.15.0/24", "dev", "tun1"], capture_output=True)
            subprocess.run(["ip", "route", "add", "104.24.14.0/24", "dev", "tun1"], capture_output=True)
            subprocess.run(["ip", "route", "add", "104.24.15.0/24", "dev", "tun1"], capture_output=True)
            return True, None
    return False, f"tunnel to {hostname} did not come up within 20s"


def stop_vpn():
    """Tear down VPN tunnel."""
    subprocess.run(["killall", "openvpn"], capture_output=True)
    subprocess.run(["ip", "route", "del", "104.24.14.0/24", "dev", "tun1"], capture_output=True)
    subprocess.run(["ip", "route", "del", "104.24.15.0/24", "dev", "tun1"], capture_output=True)


def check_redirect():
    """Make the redirect check. Returns (mirror, error)."""
    try:
        resp = req.get(
            f"https://{TARGET}",
            timeout=15,
            allow_redirects=False,
            headers=HEADERS,
            verify=False,
        )
        loc = resp.headers.get("Location", "")
        mirror = _host(loc)
        if mirror and mirror != TARGET:
            return mirror, None

        if loc:
            resp2 = req.get(
                loc, timeout=15, allow_redirects=False,
                headers=HEADERS, verify=False,
            )
            loc2 = resp2.headers.get("Location", "")
            mirror2 = _host(loc2)
            if mirror2 and mirror2 != TARGET:
                return mirror2, None

        return None, f"no redirect (HTTP {resp.status_code}, Location: {loc or 'empty'})"
    except Exception as e:
        return None, str(e)


@app.route("/check")
def check():
    key = request.args.get("key", "")
    if key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    geo = request.args.get("geo", "hu").lower()

    if not vpn_lock.acquire(timeout=1):
        return jsonify({"error": "another check is in progress, try again shortly"}), 429

    try:
        ok, vpn_err = start_vpn(geo)
        if not ok:
            return jsonify({"error": f"VPN tunnel failed for {geo}: {vpn_err}"}), 502

        time.sleep(1)
        mirror, err = check_redirect()

        if mirror:
            return jsonify({"mirror": mirror, "geo": geo, "status": 301})
        return jsonify({"error": err, "geo": geo}), 502
    finally:
        stop_vpn()
        vpn_lock.release()


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
PYEOF

echo "=== Creating systemd service ==="
cat > /etc/systemd/system/redirect-checker.service <<EOF
[Unit]
Description=GEO Redirect Checker API
After=network.target

[Service]
Type=simple
Environment=REDIRECT_API_KEY=${API_KEY}
ExecStart=/usr/bin/python3 /opt/redirect_checker.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable redirect-checker
systemctl restart redirect-checker

sleep 3
echo "=== Verifying ==="
systemctl is-active redirect-checker
curl -s --max-time 5 http://localhost:8080/health || echo "Local health check failed"

echo ""
echo "=== Done! ==="
echo "API endpoint: http://<instance_public_ip>:8080/check?key=<API_KEY>&geo=hu"
