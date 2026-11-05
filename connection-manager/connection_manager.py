#!/usr/bin/env python3
"""Publish this host's network addresses and maintain its public AAAA record."""

import os
import subprocess
import sys
import time

import requests

# When systemd (or any caller) launches this script by path, the script's own
# directory — not the repo root — is prepended to sys.path. Add the repo root
# so the ``server`` package (e.g. server.dotenv) resolves regardless of how
# the script is invoked.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

"""Minimal ``.env`` loader (stdlib only — no external dependency).

Reads a ``KEY=VALUE`` file from the repo root into ``os.environ`` so every
entrypoint (``chat-webui.py``, ``self-chat.py``, ``markdown_hosting.py``)
sees the same settings whether it is launched by hand, systemd or cron.

Only values that are NOT already exported by the real environment are applied,
so a systemd ``EnvironmentFile`` or the shell always wins over the ``.env``.
"""

from pathlib import Path

def load_dotenv(path=None):
    """Load ``path`` (default: repo-root ``.env``) into ``os.environ``."""
    if path is None:
        path = "/home/palash/.env"
    path = Path(path)
    if not path.is_file():
        print("Path is not file")
        return False
    print("Path is file")
    loaded = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded = True
    return loaded


load_dotenv()

HEARTBEAT_URL = os.environ.get(
    "HEARTBEAT_URL", "http://10.66.66.1:9863/heartbeat"
)
GODADDY_API_KEY = os.environ.get("GODADDY_API_KEY", "")
GODADDY_API_SECRET = os.environ.get("GODADDY_API_SECRET", "")
DDNS_DOMAIN = os.environ.get("DDNS_DOMAIN", "palashkantikundu.in")
DDNS_SUBDOMAIN = os.environ.get("DDNS_SUBDOMAIN", "home")
DDNS_CHECK_INTERVAL = int(os.environ.get("DDNS_CHECK_INTERVAL", "300"))

HEARTBEAT_INTERVAL = 10
_public_ipv4_cache = {"ip": None, "ts": 0.0}
_last_dns_check = 0.0
_last_known_ipv6 = None

print(GODADDY_API_KEY, GODADDY_API_SECRET)

def _get_current_ipv6():
    """Get this machine's stable global IPv6 address."""
    try:
        iface = subprocess.check_output(
            "ip -6 route show default | awk '{print $5; exit}'",
            shell=True,
            text=True,
            timeout=10,
        ).strip()
        output = subprocess.check_output(
            f"ip -6 addr show {iface} scope global",
            shell=True,
            text=True,
            timeout=10,
        )
        for line in output.splitlines():
            if "inet6" in line and "temporary" not in line:
                return line.split()[1].split("/")[0]
    except Exception as exc:
        print(f"[ddns] Failed to get IPv6: {exc}", flush=True)
    return None


def _get_wifi_ipv4():
    """Get this machine's LAN IPv4 on the default interface."""
    try:
        iface = subprocess.check_output(
            "ip -4 route show default | awk '{print $5; exit}'",
            shell=True,
            text=True,
            timeout=10,
        ).strip()
        output = subprocess.check_output(
            f"ip -4 addr show {iface} scope global",
            shell=True,
            text=True,
            timeout=10,
        )
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "secondary" not in line:
                return line.split()[1].split("/")[0]
    except Exception as exc:
        print(f"[heartbeat] Failed to get WiFi IPv4: {exc}", flush=True)
    return None


def _get_public_ipv4():
    """Get the public WAN IPv4, caching it for five minutes."""
    now = time.time()
    if _public_ipv4_cache["ip"] and now - _public_ipv4_cache["ts"] < 300:
        return _public_ipv4_cache["ip"]
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            response = requests.get(url, timeout=5)
            ip = response.text.strip()
            if response.status_code == 200 and ip.count(".") == 3:
                _public_ipv4_cache.update(ip=ip, ts=now)
                return ip
        except Exception:
            pass
    print("[heartbeat] Could not determine public IPv4", flush=True)
    return _public_ipv4_cache["ip"]


def _send_heartbeat():
    payload = {
        "ipv6": _get_current_ipv6(),
        "public_ipv4": _get_public_ipv4(),
        "wifi_ipv4": _get_wifi_ipv4(),
    }
    response = requests.post(HEARTBEAT_URL, json=payload, timeout=5)
    response.raise_for_status()
    print(f"[+] heartbeat sent: {payload}", flush=True)


def _update_godaddy_aaaa(new_ip):
    url = (
        f"https://api.godaddy.com/v1/domains/{DDNS_DOMAIN}/records/AAAA/"
        f"{DDNS_SUBDOMAIN}"
    )
    headers = {
        "Authorization": f"sso-key {GODADDY_API_KEY}:{GODADDY_API_SECRET}",
        "Content-Type": "application/json",
    }
    response = requests.put(
        url, headers=headers, json=[{"data": new_ip, "ttl": 600}], timeout=10
    )
    if response.status_code == 200:
        print(f"[ddns] GoDaddy AAAA updated to {new_ip}", flush=True)
        return True
    print(
        f"[ddns] GoDaddy update failed ({response.status_code}): {response.text}",
        flush=True,
    )
    return False


def _maybe_update_dns():
    global _last_dns_check, _last_known_ipv6

    if not (GODADDY_API_KEY and GODADDY_API_SECRET):
        return
    now = time.time()
    interval = DDNS_CHECK_INTERVAL or 300
    if now - _last_dns_check < interval:
        return
    _last_dns_check = now
    current_ip = _get_current_ipv6()
    if current_ip and current_ip != _last_known_ipv6:
        if _update_godaddy_aaaa(current_ip):
            _last_known_ipv6 = current_ip


def main():
    while True:
        try:
            try:
                _send_heartbeat()
            except Exception as exc:
                print(f"[-] GCP unreachable ({HEARTBEAT_URL}): {exc}", flush=True)
            _maybe_update_dns()
        except Exception as exc:
            print(f"[connection-manager] tick failed: {exc}", flush=True)
        time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    main()