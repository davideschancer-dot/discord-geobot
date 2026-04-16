"""
monitor.py — background GEO health monitor

Runs inside the same process as discord_bot.py, on a discord.ext.tasks loop.
Every N minutes:
  1. Load redirects.json to learn the active mirror for each GEO.
  2. For each GEO with `monitor: true` in config.yaml, run its
     configured check_method against that mirror.
  3. Update monitor_state.json.
  4. If state crossed up → red/orange, post a Discord alert with two
     buttons (Ignore / Mirror updated).
  5. If still red/orange after `realert_after_hours`, re-alert.

Adding a new GEO is config-only; adding a new check method means
adding a single function to CHECK_METHODS in this file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import discord
import requests
import urllib3
import yaml
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Logging — file (rotating) + stdout (journald on EC2)
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "monitor.log"

log = logging.getLogger("geobot")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)
    log.propagate = False

# ---------------------------------------------------------------------------
# Paths / config loading
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "config.yaml"
REDIRECTS_FILE = PROJECT_DIR / "redirects.json"
STATE_FILE = PROJECT_DIR / "monitor_state.json"

_cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
MONITOR_CFG = _cfg.get("monitor", {}) or {}
INTERVAL_MINUTES = int(MONITOR_CFG.get("interval_minutes", 10))
REQUEST_TIMEOUT = int(MONITOR_CFG.get("request_timeout", 30))
PROXY_ERROR_THRESHOLD = int(MONITOR_CFG.get("proxy_error_threshold", 3))
REALERT_AFTER_HOURS = int(MONITOR_CFG.get("realert_after_hours", 4))
IGNORE_DURATION_HOURS = int(MONITOR_CFG.get("ignore_duration_hours", 1))

# GEOs keyed by code, including monitor metadata.
GEOS: dict[str, dict] = {}
for _g in _cfg.get("geos", []):
    GEOS[_g["code"]] = {
        "name": _g["name"],
        "flag": _g.get("flag", ""),
        "check_method": _g.get("check_method", "http"),
        "monitor": bool(_g.get("monitor", False)),
        "asns": [str(a) for a in (_g.get("asns") or [])],
    }

# Per-method tuning (with defaults that match the previous hardcoded values)
HU_CONSECUTIVE_FAILURES = int(MONITOR_CFG.get("hu_consecutive_failures", 6))
RIPE_SCHEDULE_HOURS = list(MONITOR_CFG.get("ripe_schedule_hours", [4, 16]))
RIPE_PENDING_ATTEMPTS = int(MONITOR_CFG.get("ripe_pending_attempts", 3))
RIPE_PENDING_GAP_MINUTES = int(MONITOR_CFG.get("ripe_pending_gap_minutes", 60))
WEEKLY_RIPE_DOW = int(MONITOR_CFG.get("weekly_ripe_dow", 0))
WEEKLY_RIPE_HOUR = int(MONITOR_CFG.get("weekly_ripe_hour", 4))

# Cloudflare's canonical IP for chancer mirrors — any other resolved IP in a
# RIPE measurement is treated as an ISP DNS hijack / block page.
EXPECTED_IP = "104.24.14.93"

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
PROXY_HOST = os.getenv("PROXY_HOST", "gate.decodo.com")
PROXY_PORT = os.getenv("PROXY_PORT", "10001")
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

# RIPE Atlas — required for ripe_reliable_asn and decodo_plus_ripe_confirm.
RIPE_API_KEY = os.getenv("RIPE_API_KEY")
RIPE_API_BASE = "https://atlas.ripe.net/api/v2"

ALERT_WEBHOOK_URL = os.getenv("DISCORD_ALERT_WEBHOOK_URL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
HISTORY_LIMIT = 10


@dataclass
class GeoState:
    status: str = "unknown"              # up | red | orange | unknown
    active_mirror: str | None = None     # mirror being monitored
    last_alert_sent: str | None = None   # ISO timestamp
    last_alert_message_id: int | None = None
    ignored_until: str | None = None     # ISO timestamp
    consecutive_failures: int = 0
    last_checked: str | None = None
    last_reason: str | None = None
    # --- Per-method extensions ---
    # Pending confirmation window for ripe_reliable_asn (GR/PL).
    pending_confirmation: bool = False
    pending_first_seen: str | None = None
    pending_attempts: int = 0
    # Tracks the reliable-ASN IP we observed during pending — used to tell
    # "still hijacked" from "back to normal".
    pending_bad_ip: str | None = None
    # Last twice-daily ripe_reliable_asn check slot (ISO of date+hour) — used
    # to gate so the scheduled check only runs once per slot.
    last_ripe_slot: str | None = None
    # Last weekly RIPE sweep (ISO date) for decodo_plus_ripe_confirm GEOs.
    last_weekly_ripe: str | None = None
    # Rolling log of the last HISTORY_LIMIT check attempts for this GEO.
    # Each entry: {"at": iso, "mirror": str, "status": str, "reason": str|None}
    history: list[dict] = field(default_factory=list)

    def record(self, mirror: str, status: str, reason: str | None, at: datetime) -> None:
        self.history.append({
            "at": at.isoformat(timespec="seconds"),
            "mirror": mirror,
            "status": status,
            "reason": reason,
        })
        # Trim oldest entries.
        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "active_mirror": self.active_mirror,
            "last_alert_sent": self.last_alert_sent,
            "last_alert_message_id": self.last_alert_message_id,
            "ignored_until": self.ignored_until,
            "consecutive_failures": self.consecutive_failures,
            "last_checked": self.last_checked,
            "last_reason": self.last_reason,
            "pending_confirmation": self.pending_confirmation,
            "pending_first_seen": self.pending_first_seen,
            "pending_attempts": self.pending_attempts,
            "pending_bad_ip": self.pending_bad_ip,
            "last_ripe_slot": self.last_ripe_slot,
            "last_weekly_ripe": self.last_weekly_ripe,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GeoState":
        # Only accept keys that match dataclass fields; defaults fill the rest.
        kwargs = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**kwargs)


@dataclass
class MonitorState:
    geos: dict[str, GeoState] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "MonitorState":
        if not STATE_FILE.exists():
            return cls()
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return cls(geos={code: GeoState.from_dict(entry) for code, entry in raw.items()})

    def save(self) -> None:
        data = {code: gs.to_dict() for code, gs in self.geos.items()}
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def get(self, code: str) -> GeoState:
        if code not in self.geos:
            self.geos[code] = GeoState()
        return self.geos[code]

    def find_by_message_id(self, message_id: int) -> str | None:
        for code, gs in self.geos.items():
            if gs.last_alert_message_id == message_id:
                return code
        return None


# ---------------------------------------------------------------------------
# Check methods
# Each returns (status, reason) where status is one of: up, red, orange.
# ---------------------------------------------------------------------------
def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().lstrip("www.")


def check_http(geo_code: str, mirror: str) -> tuple[str, str | None]:
    """
    Check mirror via Decodo residential proxy in the GEO's country.
    Retries on transient proxy/SSL errors.
    """
    if not PROXY_USERNAME or not PROXY_PASSWORD:
        return "orange", "PROXY_USERNAME/PASSWORD not set"

    cc = geo_code.lower()
    proxy_url = f"http://user-{PROXY_USERNAME}-country-{cc}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"
    proxies = {"http": proxy_url, "https": proxy_url}
    url = f"https://{mirror}"

    last_exception: Exception | None = None

    for attempt in range(3):  # initial + 2 retries
        try:
            resp = requests.get(
                url,
                proxies=proxies,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                headers=HEADERS,
                verify=False,
            )
            return _evaluate_http_response(mirror, resp)

        except requests.exceptions.SSLError as e:
            last_exception = e
        except requests.exceptions.ProxyError as e:
            last_exception = e
        except requests.exceptions.Timeout as e:
            last_exception = e
        except requests.exceptions.ConnectionError as e:
            last_exception = e
        except Exception as e:
            last_exception = e
            break  # non-retryable

    # All retries exhausted
    if isinstance(last_exception, requests.exceptions.SSLError):
        return "red", f"SSL error — ISP connection reset: {str(last_exception)[:120]}"
    if isinstance(last_exception, requests.exceptions.ProxyError):
        return "orange", f"Proxy error: {str(last_exception)[:120]}"
    if isinstance(last_exception, requests.exceptions.Timeout):
        return "red", "Request timed out after retries"
    return "red", f"Connection error: {str(last_exception)[:120]}"


def _evaluate_http_response(mirror: str, resp) -> tuple[str, str | None]:
    """Classify an HTTP response against the block-signal taxonomy."""
    try:
        body = resp.text[:2500] or ""
    except Exception:
        body = ""
    code = resp.status_code
    headers = resp.headers

    # Hungarian government gambling block page
    hu_block_markers = ["SZTFH", "hozzáférhetetlenné", "hozzÃ¡fÃ©rhetetlennÃ©", "szerencsejáték", "szerencsejÃ¡tÃ©k"]
    if any(m in body for m in hu_block_markers):
        return "red", "Hungarian government gambling block page (SZTFH)"

    # Cloudflare hard block codes
    for cf_code in ("1009", "1010", "1012", "1015", "1020"):
        if f"Error {cf_code}" in body or f"error code: {cf_code}" in body:
            return "red", f"Cloudflare hard block (error {cf_code})"

    # ISP redirect hijack — final URL landed on an unrecognised host
    final_host = _host(resp.url)
    if final_host and final_host != mirror and mirror not in final_host:
        return "red", f"Redirected to unknown domain: {final_host} (likely ISP hijack)"

    # Cloudflare managed challenge — real browsers pass, treat as UP
    if headers.get("cf-mitigated"):
        return "up", None

    # 403 / 451 with no CF mitigation header = hard block
    if code in (403, 451):
        return "red", f"HTTP {code} — access denied / legal block"

    # Server errors
    if code >= 500:
        return "orange", f"HTTP {code} — server error"

    # 2xx / 3xx
    if code < 400:
        return "up", None

    # Other 4xx — unusual, not clearly a block
    return "orange", f"HTTP {code} — unexpected response"


def check_dns(geo_code: str, mirror: str) -> tuple[str, str | None]:
    """Legacy placeholder — use ripe_reliable_asn or decodo_plus_ripe_confirm."""
    return "orange", "DNS check method not yet implemented for this GEO"


# ---------------------------------------------------------------------------
# Decodo — per-ASN targeted proxy request.
# Decodo residential gateways let you target a specific ASN via the
# "user-{user}-asn-{asn}" username format. All 4 HU ASNs can be polled
# and required to agree before we call the mirror "down".
# ---------------------------------------------------------------------------
def _decodo_asn_check(asn: str, mirror: str) -> tuple[str, str | None]:
    """Fetch https://<mirror>/ via Decodo routed through ASN {asn}."""
    if not PROXY_USERNAME or not PROXY_PASSWORD:
        return "orange", "PROXY_USERNAME/PASSWORD not set"
    proxy_url = f"http://user-{PROXY_USERNAME}-asn-{asn}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"
    proxies = {"http": proxy_url, "https": proxy_url}
    url = f"https://{mirror}"
    try:
        resp = requests.get(
            url, proxies=proxies, timeout=REQUEST_TIMEOUT,
            allow_redirects=True, headers=HEADERS, verify=False,
        )
        return _evaluate_http_response(mirror, resp)
    except requests.exceptions.SSLError as e:
        return "red", f"SSL reset on AS{asn}: {str(e)[:80]}"
    except requests.exceptions.ProxyError as e:
        return "orange", f"Proxy error AS{asn}: {str(e)[:80]}"
    except requests.exceptions.Timeout:
        return "red", f"Timeout on AS{asn}"
    except requests.exceptions.ConnectionError as e:
        return "red", f"Connection error AS{asn}: {str(e)[:80]}"
    except Exception as e:
        return "orange", f"Error AS{asn}: {type(e).__name__}: {str(e)[:80]}"


# ---------------------------------------------------------------------------
# HU consensus — all 4 ASNs must report red, else "up".
# ---------------------------------------------------------------------------
def check_hu_consensus(geo_code: str, mirror: str) -> tuple[str, str | None]:
    asns = GEOS.get(geo_code, {}).get("asns", [])
    if not asns:
        return "orange", "No ASNs configured for HU consensus"

    from concurrent.futures import ThreadPoolExecutor
    results: dict[str, tuple[str, str | None]] = {}
    with ThreadPoolExecutor(max_workers=len(asns)) as pool:
        futures = {pool.submit(_decodo_asn_check, a, mirror): a for a in asns}
        for fut in futures:
            a = futures[fut]
            try:
                results[a] = fut.result(timeout=REQUEST_TIMEOUT + 5)
            except Exception as e:
                results[a] = ("orange", f"exec error: {e}")

    reds = [a for a, (s, _) in results.items() if s == "red"]
    oranges = [a for a, (s, _) in results.items() if s == "orange"]
    ups = [a for a, (s, _) in results.items() if s == "up"]

    detail = ", ".join(f"AS{a}={s}" for a, (s, _) in results.items())

    # All must be red for a confirmed block.
    if len(reds) == len(asns):
        return "red", f"All {len(asns)} HU ASNs blocked — {detail}"
    # Any UP = definitively not a nationwide block.
    if ups:
        return "up", None
    # Mix of red + orange (proxy errors) — inconclusive, escalate to orange.
    return "orange", f"HU consensus inconclusive — {detail}"


# ---------------------------------------------------------------------------
# RIPE Atlas — DNS measurements targeted at specific ASNs.
# ---------------------------------------------------------------------------
def _ripe_create_dns_measurement(mirror: str, cc: str, asn: str | None = None) -> int | None:
    """Create a one-off DNS A measurement. Returns measurement ID, or None."""
    if not RIPE_API_KEY:
        log.warning("RIPE_API_KEY not set — cannot create measurement")
        return None

    probe_spec: dict = {"requested": 5, "type": "country", "value": cc.upper()}
    if asn:
        probe_spec = {"requested": 5, "type": "asn", "value": str(asn)}

    payload = {
        "definitions": [{
            "target": mirror,
            "description": f"chancer {cc} {'AS'+asn if asn else 'country'}",
            "type": "dns",
            "af": 4,
            "query_class": "IN",
            "query_type": "A",
            "query_argument": mirror,
            "use_probe_resolver": True,
            "is_oneoff": True,
            "resolve_on_probe": True,
        }],
        "probes": [probe_spec],
    }
    try:
        resp = requests.post(
            f"{RIPE_API_BASE}/measurements/?key={RIPE_API_KEY}",
            json=payload, timeout=30,
        )
        if resp.status_code not in (200, 201):
            log.error("RIPE create failed (%s AS%s): %s %s",
                      cc, asn, resp.status_code, resp.text[:200])
            return None
        return resp.json().get("measurements", [None])[0]
    except Exception as e:
        log.error("RIPE create exception: %s", e)
        return None


def _ripe_fetch_results(measurement_id: int, wait_seconds: int = 600) -> list[dict]:
    """Poll a RIPE measurement's results until it stops changing or times out."""
    import time as _time
    deadline = _time.time() + wait_seconds
    last = []
    while _time.time() < deadline:
        try:
            resp = requests.get(
                f"{RIPE_API_BASE}/measurements/{measurement_id}/results/",
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) >= 3:
                    # 3+ probe results is enough signal for our ASN checks.
                    return data
                last = data if isinstance(data, list) else last
        except Exception as e:
            log.warning("RIPE fetch %s: %s", measurement_id, e)
        _time.sleep(15)
    return last


def _ripe_extract_ips(result: dict) -> list[str]:
    """Parse DNS A records out of a single RIPE DNS result row."""
    ips: list[str] = []
    for rk in ("resultset", "result"):
        entries = result.get(rk) or []
        if not isinstance(entries, list):
            entries = [entries]
        for entry in entries:
            for ans in (entry.get("answers") or []):
                if ans.get("TYPE") == "A" and ans.get("RDATA"):
                    rd = ans["RDATA"]
                    if isinstance(rd, list):
                        ips.extend(rd)
                    else:
                        ips.append(rd)
    return ips


def _ripe_hijack_rate(results: list[dict]) -> tuple[float, int, int]:
    """Return (hijack_rate, hijacked_count, total). 1.0 = all probes hijacked."""
    total = 0
    hijacked = 0
    for r in results:
        ips = _ripe_extract_ips(r)
        if not ips:
            continue
        total += 1
        if EXPECTED_IP not in ips:
            hijacked += 1
    rate = (hijacked / total) if total else 0.0
    return rate, hijacked, total


def _ripe_run_asn(mirror: str, cc: str, asn: str) -> tuple[float, int, int, str | None]:
    """Run a per-ASN RIPE DNS measurement. Returns (rate, hijacked, total, err)."""
    mid = _ripe_create_dns_measurement(mirror, cc, asn)
    if not mid:
        return 0.0, 0, 0, "RIPE create failed"
    results = _ripe_fetch_results(mid, wait_seconds=600)
    if not results:
        return 0.0, 0, 0, f"No RIPE results for measurement {mid}"
    rate, hj, tot = _ripe_hijack_rate(results)
    return rate, hj, tot, None


# ---------------------------------------------------------------------------
# GR/PL — ripe_reliable_asn
# The first ASN in geo.asns is the RELIABLE one (OTE/Cosmote, Orange PL).
# If that ASN resolves to anything other than EXPECTED_IP, we open a pending
# window. The window needs RIPE_PENDING_ATTEMPTS confirmations (spaced by
# RIPE_PENDING_GAP_MINUTES) before we report red.
#
# This function is *stateless* — it only returns the "current reliable-ASN
# observation". run_monitor_cycle holds the pending window state.
# ---------------------------------------------------------------------------
def check_ripe_reliable_asn(geo_code: str, mirror: str) -> tuple[str, str | None]:
    asns = GEOS.get(geo_code, {}).get("asns", [])
    if not asns:
        return "orange", f"No ASNs configured for {geo_code}"
    reliable = asns[0]
    rate, hj, tot, err = _ripe_run_asn(mirror, geo_code, reliable)
    if err:
        return "orange", f"AS{reliable} RIPE error: {err}"
    if tot == 0:
        return "orange", f"AS{reliable} RIPE returned no probes"
    if rate >= 1.0:
        return "red", f"AS{reliable} 100% hijacked ({hj}/{tot} probes, expected {EXPECTED_IP})"
    if rate > 0:
        return "orange", f"AS{reliable} partial hijack {hj}/{tot}"
    return "up", None


# ---------------------------------------------------------------------------
# DK/NO/FR/AE — decodo_plus_ripe_confirm
# Decodo HTTP is the primary signal (runs every cycle). When the primary
# crosses the failure threshold, we escalate to the 4-ASN RIPE confirm —
# ALL configured ASNs must be 100% hijacked for us to report red.
# run_monitor_cycle triggers _run_4asn_confirm when needed.
# ---------------------------------------------------------------------------
def _run_4asn_confirm(geo_code: str, mirror: str) -> tuple[str, str | None]:
    asns = GEOS.get(geo_code, {}).get("asns", [])
    if not asns:
        return "orange", f"No ASNs configured for {geo_code} confirm"
    from concurrent.futures import ThreadPoolExecutor
    per_asn: dict[str, tuple[float, int, int, str | None]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(asns))) as pool:
        futs = {pool.submit(_ripe_run_asn, mirror, geo_code, a): a for a in asns}
        for fut in futs:
            a = futs[fut]
            try:
                per_asn[a] = fut.result(timeout=720)
            except Exception as e:
                per_asn[a] = (0.0, 0, 0, f"exec: {e}")
    fully = [a for a, (r, _, t, e) in per_asn.items() if e is None and t > 0 and r >= 1.0]
    detail = ", ".join(f"AS{a}={int(r*100)}%" for a, (r, _, _, _) in per_asn.items())
    if len(fully) == len(asns):
        return "red", f"All {len(asns)} ASNs 100% hijacked — {detail}"
    return "up", f"4-ASN confirm negative — {detail}"


def check_decodo_plus_ripe_confirm(geo_code: str, mirror: str) -> tuple[str, str | None]:
    """Primary fast path: same as check_http (Decodo country-level proxy)."""
    return check_http(geo_code, mirror)


# Registry — adding a new check method is just a new function + entry here.
CHECK_METHODS: dict[str, Callable[[str, str], tuple[str, str | None]]] = {
    "http": check_http,
    "dns": check_dns,
    "hu_consensus": check_hu_consensus,
    "ripe_reliable_asn": check_ripe_reliable_asn,
    "decodo_plus_ripe_confirm": check_decodo_plus_ripe_confirm,
}


# ---------------------------------------------------------------------------
# Alert view (persistent — survives bot restarts)
# ---------------------------------------------------------------------------
class AlertView(discord.ui.View):
    """
    Attached to every monitor alert message. Buttons use fixed custom_ids so
    the view survives bot restarts (registered via bot.add_view()).
    The GEO is looked up by matching interaction.message.id against state.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Ignore",
        style=discord.ButtonStyle.secondary,
        custom_id="monitor_alert:ignore",
    )
    async def ignore(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = MonitorState.load()
        code = state.find_by_message_id(interaction.message.id)
        if not code:
            await interaction.response.send_message(
                "This alert is no longer tracked in monitor state.",
                ephemeral=True,
            )
            return

        gs = state.get(code)
        until = datetime.now(timezone.utc) + timedelta(hours=IGNORE_DURATION_HOURS)
        gs.ignored_until = until.isoformat(timespec="seconds")
        state.save()

        geo_info = GEOS.get(code, {"name": code, "flag": ""})
        await interaction.response.send_message(
            f"🔕 Alerts for {geo_info['flag']} **{geo_info['name']}** paused for "
            f"{IGNORE_DURATION_HOURS}h (until {until.strftime('%H:%M UTC')})."
        )

    @discord.ui.button(
        label="Mirror updated",
        style=discord.ButtonStyle.primary,
        custom_id="monitor_alert:mirror_updated",
    )
    async def mirror_updated(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = MonitorState.load()
        code = state.find_by_message_id(interaction.message.id)
        if not code:
            await interaction.response.send_message(
                "This alert is no longer tracked in monitor state.",
                ephemeral=True,
            )
            return

        # Re-read redirects.json for the (presumably updated) mirror
        redirects = {}
        if REDIRECTS_FILE.exists():
            redirects = json.loads(REDIRECTS_FILE.read_text(encoding="utf-8"))
        new_mirror = (redirects.get(code) or {}).get("mirror")
        if not new_mirror:
            await interaction.response.send_message(
                f"No mirror set for `{code}` in redirects.json. "
                f"Run `/check-redirect geo:{code}` or `/set-redirect` first.",
                ephemeral=True,
            )
            return

        gs = state.get(code)
        gs.active_mirror = new_mirror
        gs.status = "unknown"
        gs.consecutive_failures = 0
        gs.last_alert_sent = None
        gs.last_alert_message_id = None
        gs.ignored_until = None
        state.save()

        geo_info = GEOS.get(code, {"name": code, "flag": ""})
        await interaction.response.send_message(
            f"🔄 Now monitoring {geo_info['flag']} **{geo_info['name']}** against `{new_mirror}`. "
            f"Next check in the upcoming cycle."
        )


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------
def _status_emoji(status: str) -> str:
    return {"red": "🔴", "orange": "🟠", "up": "✅", "unknown": "❓"}.get(status, "❓")


async def _send_alert(
    bot: discord.Client,
    code: str,
    mirror: str,
    status: str,
    reason: str,
    first_detected: datetime,
) -> int | None:
    """
    Send an alert via the configured webhook, attaching the persistent view.
    Returns the message_id so state can track it.
    """
    if not ALERT_WEBHOOK_URL:
        log.warning("DISCORD_ALERT_WEBHOOK_URL not set — skipping alert for %s", code)
        return None

    geo_info = GEOS.get(code, {"name": code, "flag": ""})
    emoji = _status_emoji(status)
    level = "DOWN" if status == "red" else "UNCERTAIN"

    content = (
        f"{emoji} {geo_info['flag']} **{geo_info['name']} {level}**\n"
        f"Mirror: `{mirror}`\n"
        f"Reason: {reason}\n"
        f"First detected: {first_detected.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    try:
        # aiohttp is bundled with discord.py
        import aiohttp
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(ALERT_WEBHOOK_URL, session=session, client=bot)
            msg = await webhook.send(
                content=content,
                view=AlertView(),
                wait=True,
            )
            return msg.id
    except Exception as e:
        log.error("Failed to send alert for %s: %s", code, e)
        return None


# ---------------------------------------------------------------------------
# Monitor cycle
# ---------------------------------------------------------------------------
def _load_redirects() -> dict:
    if REDIRECTS_FILE.exists():
        return json.loads(REDIRECTS_FILE.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------------------
# Per-method cycle helpers
# Each returns (effective_status, reason, should_alert) after updating gs
# in place. Status "skip" means: don't write alert/history for this cycle.
# ---------------------------------------------------------------------------
def _ripe_slot_id(now: datetime) -> str | None:
    """Return the current scheduled slot id if `now` falls inside one.
    We say a slot is "open" for the first 10 minutes after the scheduled hour
    so a single scheduled cycle catches it."""
    if now.hour in RIPE_SCHEDULE_HOURS and now.minute < 10:
        return f"{now.date().isoformat()}T{now.hour:02d}"
    return None


async def _cycle_hu_consensus(loop, gs: GeoState, code: str, mirror: str
                              ) -> tuple[str, str | None, bool]:
    status, reason = await loop.run_in_executor(None, check_hu_consensus, code, mirror)
    if status == "up":
        gs.consecutive_failures = 0
        effective = "up"
    elif status == "red":
        gs.consecutive_failures += 1
        # Only surface red once the consensus has persisted long enough.
        if gs.consecutive_failures >= HU_CONSECUTIVE_FAILURES:
            effective = "red"
        else:
            effective = gs.status if gs.status in ("red", "orange") else "unknown"
    else:
        gs.consecutive_failures += 1
        effective = gs.status if gs.status in ("red", "orange") else "unknown"
    alert = (effective == "red" and gs.status != "red")
    return effective, reason, alert


async def _cycle_ripe_reliable_asn(loop, gs: GeoState, code: str, mirror: str,
                                    now: datetime) -> tuple[str, str | None, bool]:
    slot = _ripe_slot_id(now)
    due_scheduled = (slot is not None and gs.last_ripe_slot != slot)

    due_pending = False
    if gs.pending_confirmation and gs.pending_first_seen:
        try:
            last = datetime.fromisoformat(gs.last_checked) if gs.last_checked else None
        except ValueError:
            last = None
        if last is None or (now - last).total_seconds() >= RIPE_PENDING_GAP_MINUTES * 60:
            due_pending = True

    if not (due_scheduled or due_pending):
        return "skip", None, False

    status, reason = await loop.run_in_executor(None, check_ripe_reliable_asn, code, mirror)
    if due_scheduled:
        gs.last_ripe_slot = slot

    if status == "up":
        cleared = gs.pending_confirmation
        gs.pending_confirmation = False
        gs.pending_first_seen = None
        gs.pending_attempts = 0
        gs.pending_bad_ip = None
        return "up", ("pending cleared" if cleared else None), False

    if status == "red":
        if not gs.pending_confirmation:
            gs.pending_confirmation = True
            gs.pending_first_seen = now.isoformat(timespec="seconds")
            gs.pending_attempts = 1
            return "orange", f"pending 1/{RIPE_PENDING_ATTEMPTS}: {reason}", False
        gs.pending_attempts += 1
        if gs.pending_attempts >= RIPE_PENDING_ATTEMPTS:
            alert = (gs.status != "red")
            return "red", f"confirmed after {gs.pending_attempts} attempts — {reason}", alert
        return "orange", f"pending {gs.pending_attempts}/{RIPE_PENDING_ATTEMPTS}: {reason}", False

    # status == orange
    return gs.status if gs.status in ("up", "red", "orange") else "orange", reason, False


async def _cycle_decodo_plus_ripe(loop, gs: GeoState, code: str, mirror: str,
                                   now: datetime) -> tuple[str, str | None, bool]:
    # Primary fast path (Decodo country-level proxy).
    status, reason = await loop.run_in_executor(None, check_http, code, mirror)

    weekly_due = (
        now.weekday() == WEEKLY_RIPE_DOW
        and now.hour == WEEKLY_RIPE_HOUR
        and now.minute < 10
        and gs.last_weekly_ripe != now.date().isoformat()
    )

    if status == "up" and not weekly_due:
        gs.consecutive_failures = 0
        return "up", None, False

    if status == "red":
        gs.consecutive_failures += 1
    elif status == "orange":
        gs.consecutive_failures += 1

    trigger_confirm = (status == "red" and gs.consecutive_failures >= PROXY_ERROR_THRESHOLD) or weekly_due
    if not trigger_confirm:
        effective = "orange" if gs.consecutive_failures >= PROXY_ERROR_THRESHOLD else (
            gs.status if gs.status in ("up", "red", "orange") else "unknown"
        )
        return effective, reason, False

    # 4-ASN confirm — only escalate if ALL ASNs are 100% hijacked.
    if weekly_due:
        gs.last_weekly_ripe = now.date().isoformat()
    confirm_status, confirm_reason = await loop.run_in_executor(None, _run_4asn_confirm, code, mirror)
    combined = f"decodo={status} ({reason}); confirm={confirm_status} ({confirm_reason})"
    if confirm_status == "red":
        alert = (gs.status != "red")
        return "red", combined, alert
    return "up" if status == "up" else "orange", combined, False


async def run_monitor_cycle(bot: discord.Client) -> None:
    """
    One pass over every enabled GEO. Each method decides whether it actually
    runs this cycle (cadence gating lives in the per-method helper).
    Runs sync check functions in a thread executor so the event loop isn't blocked.
    """
    state = MonitorState.load()
    redirects = _load_redirects()
    now = datetime.now(timezone.utc)
    loop = asyncio.get_running_loop()

    for code, geo in GEOS.items():
        if not geo["monitor"]:
            continue
        gs = state.get(code)

        # Respect ignore window
        if gs.ignored_until:
            try:
                ignored_until = datetime.fromisoformat(gs.ignored_until)
                if ignored_until > now:
                    log.info("%s ignored until %s — skipping", code, gs.ignored_until)
                    continue
                gs.ignored_until = None
            except ValueError:
                gs.ignored_until = None

        mirror = (redirects.get(code) or {}).get("mirror")
        if not mirror:
            log.warning("%s: no mirror in redirects.json — skipping", code)
            continue

        method_name = geo["check_method"]
        prev_status = gs.status

        try:
            if method_name == "hu_consensus":
                effective, reason, should_alert = await _cycle_hu_consensus(loop, gs, code, mirror)
            elif method_name == "ripe_reliable_asn":
                effective, reason, should_alert = await _cycle_ripe_reliable_asn(loop, gs, code, mirror, now)
            elif method_name == "decodo_plus_ripe_confirm":
                effective, reason, should_alert = await _cycle_decodo_plus_ripe(loop, gs, code, mirror, now)
            elif method_name in CHECK_METHODS:
                # Legacy path (http / dns / anything else registered).
                method = CHECK_METHODS[method_name]
                status, reason = await loop.run_in_executor(None, method, code, mirror)
                if status == "up":
                    gs.consecutive_failures = 0
                    effective = "up"
                elif status == "red":
                    gs.consecutive_failures += 1
                    effective = "red"
                else:
                    gs.consecutive_failures += 1
                    effective = "orange" if gs.consecutive_failures >= PROXY_ERROR_THRESHOLD else (
                        gs.status if gs.status in ("up", "red", "orange") else "unknown"
                    )
                should_alert = (effective in ("red", "orange") and effective != prev_status)
            else:
                log.error("%s: unknown check_method '%s'", code, method_name)
                continue
        except Exception as e:
            effective, reason, should_alert = "orange", f"Cycle raised {type(e).__name__}: {e}", False

        if effective == "skip":
            # Cadence gate said "not this cycle" — don't touch alert state.
            continue

        # Re-alert window: even if status didn't change, re-alert after N hours.
        if not should_alert and effective in ("red", "orange") and gs.last_alert_sent:
            try:
                last = datetime.fromisoformat(gs.last_alert_sent)
                if (now - last).total_seconds() >= REALERT_AFTER_HOURS * 3600:
                    should_alert = True
            except ValueError:
                pass

        gs.status = effective
        gs.active_mirror = mirror
        gs.last_checked = now.isoformat(timespec="seconds")
        gs.last_reason = reason
        gs.record(mirror=mirror, status=effective, reason=reason, at=now)

        if should_alert:
            message_id = await _send_alert(
                bot=bot, code=code, mirror=mirror,
                status=effective, reason=reason or "(no detail)", first_detected=now,
            )
            gs.last_alert_sent = now.isoformat(timespec="seconds")
            if message_id is not None:
                gs.last_alert_message_id = message_id

        log.info(
            "%s mirror=%s method=%s effective=%s fails=%d alert=%s reason=%s",
            code, mirror, method_name, effective, gs.consecutive_failures,
            "yes" if should_alert else "no", reason,
        )

    state.save()


# ---------------------------------------------------------------------------
# Ad-hoc (independent) check — used by /monitor-check and /trigger-monitor.
# Does NOT touch monitor_state.json or fire alerts; returns raw result.
# ---------------------------------------------------------------------------
async def run_adhoc_check(
    geo_code: str,
    mirror: str,
    check_method: str | None = None,
) -> tuple[str, str | None]:
    """Run a single check and return (status, reason). Safe to call from the
    event loop; wraps the blocking sync check in a thread executor."""
    code = geo_code.upper()
    geo = GEOS.get(code)
    method_name = check_method or (geo["check_method"] if geo else "http")
    method = CHECK_METHODS.get(method_name)
    if method is None:
        return "orange", f"unknown check_method '{method_name}'"

    loop = asyncio.get_running_loop()
    try:
        status, reason = await loop.run_in_executor(None, method, code, mirror)
    except Exception as e:
        status, reason = "orange", f"{type(e).__name__}: {e}"

    log.info("adhoc-check %s mirror=%s method=%s status=%s reason=%s",
             code, mirror, method_name, status, reason)
    return status, reason


# ---------------------------------------------------------------------------
# Background task wiring
# ---------------------------------------------------------------------------
def create_monitor_task(bot: discord.Client) -> tasks.Loop:
    """
    Create a discord.ext.tasks loop that runs the monitor cycle on the
    configured interval. Caller is responsible for .start()ing it after
    the bot is ready.
    """
    @tasks.loop(minutes=INTERVAL_MINUTES)
    async def monitor_loop():
        try:
            await run_monitor_cycle(bot)
        except Exception as e:
            print(f"[monitor] Unexpected error in cycle: {type(e).__name__}: {e}", flush=True)

    @monitor_loop.before_loop
    async def wait_until_ready():
        await bot.wait_until_ready()

    return monitor_loop
