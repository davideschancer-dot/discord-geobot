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
import base64
import concurrent.futures
import json
import logging
import os
import struct
import time
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
FAILURE_THRESHOLD_HU = int(MONITOR_CFG.get("failure_threshold_hu", 6))

# GEOs keyed by code, including monitor metadata.
GEOS: dict[str, dict] = {}
for _g in _cfg.get("geos", []):
    GEOS[_g["code"]] = {
        "name": _g["name"],
        "flag": _g.get("flag", ""),
        "check_method": _g.get("check_method", "http"),
        "monitor": bool(_g.get("monitor", False)),
        "asns": _g.get("asns", []),
    }

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
PROXY_HOST = os.getenv("PROXY_HOST", "gate.decodo.com")
PROXY_PORT = os.getenv("PROXY_PORT", "10001")
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

RIPE_ATLAS_API_KEY = os.getenv("RIPE_ATLAS_API_KEY")

ALERT_WEBHOOK_URL = os.getenv("DISCORD_ALERT_WEBHOOK_URL")
# Preferred: post alerts as the bot to this channel ID. Required for buttons —
# Discord rejects interactive components on messages from non-application
# webhooks. Falls back to the webhook (without buttons) if unset.
ALERT_CHANNEL_ID = os.getenv("DISCORD_ALERT_CHANNEL_ID")

# Expected Cloudflare IP for mirrors — anything else = DNS hijack
EXPECTED_IP = "104.24.14.93"

# Reliable single ASN per GEO. GR/PL use this for pending-confirmation entry;
# DK/FR/NO/AE use it for the once-daily single-ASN check (Tier 3).
RELIABLE_ASN = {
    "GR": "1241",    # OTE / Cosmote
    "PL": "5617",    # Orange Polska
    "DK": "3292",    # TDC / Nuuday
    "NO": "5381",    # Telenor Norway
    "FR": "3215",    # Orange France
    "AE": "15412",   # FLAG Telecom / Etisalat
}

# GR/PL pending-confirmation parameters
PENDING_RETRY_MINUTES = 60
PENDING_MAX_ATTEMPTS = 3

# GR/PL scheduled check times (UTC hours)
RIPE_SCHEDULE_HOURS = [4, 16]

# DK/FR/NO/AE — daily single-ASN RIPE check fires once at this UTC hour
DAILY_RIPE_HOUR = 4

# RIPE credit floor — measurements are skipped when current balance falls
# below this threshold so we never silently exhaust credits or starve
# higher-priority geos. Tunable via monitor.ripe_credit_floor in config.yaml.
RIPE_CREDIT_FLOOR = int(MONITOR_CFG.get("ripe_credit_floor", 50))

# Kept only because /mirror-test still surfaces a 4-ASN snapshot for
# stakeholders. The live monitor no longer escalates to a 4-ASN confirm
# (Tier 3 geos run the daily single-ASN check instead).
CONFIRM_ASNS = {
    "DK": ["3292", "3308", "9158", "31027"],     # TDC, Telenor DK, Stofa/Norlys, Hiper
    "NO": ["2116", "5381", "12929", "29695"],     # Uninett/Sikt, Telenor Norway, Telia Norway, Get/Telia
    "FR": ["3215", "5410", "12322", "15557"],     # Orange FR, Bouygues, Free/ProXad, SFR
    "AE": ["5384", "15412", "15802", "45102"],    # du/EITC, FLAG/Etisalat, Etisalat, Alibaba UAE
}

# UAE block pages are served at these Cloudflare IPs — NOT the real site
UAE_BLOCK_IPS = {"104.16.130.238", "104.16.131.238"}

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
    status: str = "unknown"              # up | red | orange | pending | unknown
    active_mirror: str | None = None     # mirror being monitored
    last_alert_sent: str | None = None   # ISO timestamp
    last_alert_message_id: int | None = None
    ignored_until: str | None = None     # ISO timestamp
    consecutive_failures: int = 0
    last_checked: str | None = None
    last_reason: str | None = None
    # GR/PL pending-confirmation window
    pending_confirmation: bool = False
    pending_attempts: int = 0
    pending_first_seen: str | None = None  # ISO timestamp
    last_blocked_asns: list[str] = field(default_factory=list)
    alert_fired: bool = False              # True = alert sent for this outage
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
        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]

    def clear_pending(self) -> None:
        """Reset pending-confirmation state (used on recovery or after alert)."""
        self.pending_confirmation = False
        self.pending_attempts = 0
        self.pending_first_seen = None
        self.last_blocked_asns = []

    def clear_outage(self) -> None:
        """Silently clear all outage state (no 'up' notification sent)."""
        self.status = "up"
        self.consecutive_failures = 0
        self.alert_fired = False
        self.last_alert_sent = None
        self.last_alert_message_id = None
        self.clear_pending()

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
            "pending_attempts": self.pending_attempts,
            "pending_first_seen": self.pending_first_seen,
            "last_blocked_asns": self.last_blocked_asns,
            "alert_fired": self.alert_fired,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GeoState":
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
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


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
    """
    DNS check via RIPE Atlas probes — not yet implemented.
    Poland phase will implement this: detects ISP DNS hijack by comparing
    resolved IP to the real Cloudflare IP 104.24.14.93.
    """
    return "orange", "DNS check method not yet implemented for this GEO"


def _check_single_asn(geo_code: str, mirror: str, asn: str) -> tuple[str, str | None, str]:
    """
    Run a Decodo HTTP check routed through a specific ASN.
    Returns (status, reason, asn).
    """
    if not PROXY_USERNAME or not PROXY_PASSWORD:
        return "orange", "PROXY_USERNAME/PASSWORD not set", asn

    cc = geo_code.lower()
    proxy_url = (
        f"http://user-{PROXY_USERNAME}-country-{cc}-asn-{asn}"
        f":{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"
    )
    proxies = {"http": proxy_url, "https": proxy_url}
    url = f"https://{mirror}"

    last_exception: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(
                url,
                proxies=proxies,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                headers=HEADERS,
                verify=False,
            )
            status, reason = _evaluate_http_response(mirror, resp)
            return status, reason, asn
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
            break

    if isinstance(last_exception, requests.exceptions.SSLError):
        return "red", f"SSL error — ISP connection reset: {str(last_exception)[:120]}", asn
    if isinstance(last_exception, requests.exceptions.ProxyError):
        return "orange", f"Proxy error: {str(last_exception)[:120]}", asn
    if isinstance(last_exception, requests.exceptions.Timeout):
        return "red", "Request timed out after retries", asn
    return "red", f"Connection error: {str(last_exception)[:120]}", asn


def check_hu_consensus(geo_code: str, mirror: str) -> tuple[str, str | None]:
    """
    Hungary all-ASN consensus check. Runs each configured ASN in parallel
    via ThreadPoolExecutor. Returns a synthetic status:
      - "blocked" if ALL ASNs report the mirror as blocked (red)
      - "up" if at least one ASN sees it as accessible
      - "inconclusive" if results are mixed proxy errors / unknowns
    The caller (run_monitor_cycle) handles the 6-cycle threshold.
    """
    asns = GEOS.get(geo_code, {}).get("asns", [])
    if not asns:
        return check_http(geo_code, mirror)

    log.info("HU consensus check: %s across ASNs %s", mirror, asns)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(asns)) as pool:
        futures = {
            pool.submit(_check_single_asn, geo_code, mirror, asn): asn
            for asn in asns
        }
        results: list[tuple[str, str | None, str]] = []
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    blocked = [r for r in results if r[0] == "red"]
    up = [r for r in results if r[0] == "up"]
    orange = [r for r in results if r[0] == "orange"]

    for status, reason, asn in results:
        log.info("  ASN %s: %s — %s", asn, status, reason or "OK")

    if up:
        return "up", None
    if len(blocked) == len(asns):
        reasons = [f"AS{r[2]}: {r[1]}" for r in blocked]
        return "blocked", f"All {len(asns)} ASNs blocked — {'; '.join(reasons)}"
    # Mixed: some proxy errors, some blocked — don't count as failure
    return "inconclusive", f"Mixed results: {len(blocked)} blocked, {len(orange)} proxy errors"


# ---------------------------------------------------------------------------
# RIPE Atlas DNS measurement client
# ---------------------------------------------------------------------------
RIPE_API_BASE = "https://atlas.ripe.net/api/v2"

# Credit-balance cache. (balance, fetched_at_monotonic)
_RIPE_CREDIT_CACHE: dict[str, float | int | None] = {"balance": None, "at": 0.0}
_RIPE_CREDIT_TTL_SECONDS = 60


def _ripe_credits_available() -> int | None:
    """
    Return current RIPE Atlas credit balance, or None on error / no API key.
    Cached for _RIPE_CREDIT_TTL_SECONDS so a single cycle's worth of
    measurement creations doesn't hammer the credits endpoint.
    """
    if not RIPE_ATLAS_API_KEY:
        return None
    now = time.monotonic()
    cached_at = _RIPE_CREDIT_CACHE.get("at") or 0.0
    if now - cached_at < _RIPE_CREDIT_TTL_SECONDS:
        cached = _RIPE_CREDIT_CACHE.get("balance")
        if isinstance(cached, int):
            return cached
    try:
        resp = requests.get(
            f"{RIPE_API_BASE}/credits/",
            headers={"Authorization": f"Key {RIPE_ATLAS_API_KEY}"},
            timeout=10,
        )
        if resp.status_code == 200:
            balance = int(resp.json().get("current_balance", 0))
            _RIPE_CREDIT_CACHE["balance"] = balance
            _RIPE_CREDIT_CACHE["at"] = now
            return balance
        log.warning("RIPE credits endpoint returned HTTP %d", resp.status_code)
    except Exception as e:
        log.warning("Failed to read RIPE credit balance: %s", e)
    return None


def _ripe_create_measurement(
    domain: str,
    country_code: str,
    asn: str,
    probe_count: int = 5,
) -> int | None:
    """
    Create a one-off RIPE Atlas DNS A-record measurement for `domain`,
    targeting probes in `country_code` on `asn`. Returns measurement ID.
    Skipped (returns None with a logged reason) when the credit balance is
    below RIPE_CREDIT_FLOOR — never silently exhaust credits.
    """
    if not RIPE_ATLAS_API_KEY:
        log.warning("RIPE_ATLAS_API_KEY not set — cannot create measurement")
        return None

    balance = _ripe_credits_available()
    if balance is not None and balance < RIPE_CREDIT_FLOOR:
        log.warning(
            "ripe_skipped reason=low_credit balance=%d floor=%d cc=%s asn=%s",
            balance, RIPE_CREDIT_FLOOR, country_code, asn,
        )
        return None

    payload = {
        "definitions": [{
            "type": "dns",
            "af": 4,
            "query_class": "IN",
            "query_type": "A",
            "query_argument": domain,
            "use_probe_resolver": True,
            "description": f"GEO monitor: {domain} from AS{asn} in {country_code}",
            "use_macros": False,
            "protocol": "UDP",
            "udp_payload_size": 512,
            "set_rd_bit": True,
            "is_oneoff": True,
        }],
        "probes": [{
            "requested": probe_count,
            "type": "asn",
            "value": str(asn),
            "tags": {"include": [], "exclude": []},
        }],
    }

    try:
        resp = requests.post(
            f"{RIPE_API_BASE}/measurements/",
            json=payload,
            headers={"Authorization": f"Key {RIPE_ATLAS_API_KEY}"},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            msm_id = data.get("measurements", [None])[0]
            log.info("RIPE measurement created: %s (AS%s in %s)", msm_id, asn, country_code)
            return msm_id
        else:
            log.error("RIPE create failed: HTTP %d — %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        log.error("RIPE create error: %s", e)
        return None


def _ripe_poll_results(
    measurement_id: int,
    max_wait: int = 120,
    poll_interval: int = 10,
) -> list[dict]:
    """
    Poll a RIPE Atlas measurement until results arrive or timeout.
    Returns list of result dicts.
    """
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                f"{RIPE_API_BASE}/measurements/{measurement_id}/results/",
                timeout=15,
            )
            if resp.status_code == 200:
                results = resp.json()
                if results:
                    return results
        except Exception as e:
            log.warning("RIPE poll error for %d: %s", measurement_id, e)
        time.sleep(poll_interval)
    log.warning("RIPE measurement %d timed out after %ds", measurement_id, max_wait)
    return []


def _parse_abuf_ips(abuf_b64: str) -> list[str]:
    """Extract A-record IPs from a base64-encoded DNS response buffer."""
    try:
        buf = base64.b64decode(abuf_b64)
        if len(buf) < 12:
            return []
        qdcount = struct.unpack("!H", buf[4:6])[0]
        ancount = struct.unpack("!H", buf[6:8])[0]
        # Skip question section
        offset = 12
        for _ in range(qdcount):
            while offset < len(buf):
                length = buf[offset]
                if length == 0:
                    offset += 1
                    break
                if length >= 192:  # compression pointer
                    offset += 2
                    break
                offset += 1 + length
            offset += 4  # QTYPE + QCLASS
        # Parse answer records
        ips = []
        for _ in range(ancount):
            if offset >= len(buf):
                break
            # Skip NAME (may be compressed)
            if buf[offset] >= 192:
                offset += 2
            else:
                while offset < len(buf):
                    length = buf[offset]
                    if length == 0:
                        offset += 1
                        break
                    if length >= 192:
                        offset += 2
                        break
                    offset += 1 + length
            if offset + 10 > len(buf):
                break
            rtype, _, _, rdlength = struct.unpack("!HHIH", buf[offset:offset + 10])
            offset += 10
            if rtype == 1 and rdlength == 4:  # A record
                ips.append(".".join(str(b) for b in buf[offset:offset + 4]))
            offset += rdlength
        return ips
    except Exception:
        return []


def _ripe_extract_ips(results: list[dict]) -> list[str]:
    """Extract resolved A-record IPs from RIPE Atlas DNS measurement results."""
    ips = []
    for result in results:
        # RIPE results use "resultset" (array of sub-measurements per probe)
        resultset = result.get("resultset") or []
        if not resultset:
            # Fallback: single "result" dict at top level
            r = result.get("result")
            if isinstance(r, dict):
                resultset = [{"result": r}]
        for entry in resultset:
            if not isinstance(entry, dict):
                continue
            sub = entry.get("result", {})
            if not isinstance(sub, dict):
                continue
            # Primary: decode abuf (base64 DNS wire format)
            abuf = sub.get("abuf")
            if abuf:
                ips.extend(_parse_abuf_ips(abuf))
                continue
            # Fallback: pre-decoded answers (some older probe firmware)
            for ans in sub.get("answers", []):
                if ans.get("TYPE") == "A" and ans.get("RDATA"):
                    ips.append(ans["RDATA"])
    return ips


def ripe_check_asn(
    domain: str,
    country_code: str,
    asn: str,
) -> dict:
    """
    Run a RIPE Atlas DNS check for a single ASN.
    Returns {"asn": str, "hijacked": bool, "ips": list[str], "error": str|None}
    """
    msm_id = _ripe_create_measurement(domain, country_code, asn)
    if msm_id is None:
        return {"asn": asn, "hijacked": False, "ips": [], "error": "Failed to create measurement"}

    results = _ripe_poll_results(msm_id)
    if not results:
        return {"asn": asn, "hijacked": False, "ips": [], "error": "No results returned"}

    ips = _ripe_extract_ips(results)
    if not ips:
        return {"asn": asn, "hijacked": False, "ips": [], "error": "No A records resolved"}

    # Any IP that differs from expected = hijacked
    hijacked = any(ip != EXPECTED_IP for ip in ips)
    return {"asn": asn, "hijacked": hijacked, "ips": list(set(ips)), "error": None}


def ripe_check_per_asn(
    domain: str,
    country_code: str,
    asns: list[str],
) -> dict:
    """
    Run RIPE Atlas DNS checks across multiple ASNs in parallel.
    Returns {
        "per_asn": {asn: {hijacked, ips, error}},
        "hijacked_asns": [asn, ...],
        "all_hijacked": bool,
    }
    """
    log.info("RIPE per-ASN check: %s in %s across ASNs %s", domain, country_code, asns)
    per_asn = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(asns)) as pool:
        futures = {
            pool.submit(ripe_check_asn, domain, country_code, asn): asn
            for asn in asns
        }
        for fut in concurrent.futures.as_completed(futures):
            result = fut.result()
            per_asn[result["asn"]] = result
            log.info("  RIPE AS%s: hijacked=%s ips=%s err=%s",
                     result["asn"], result["hijacked"], result["ips"], result["error"])

    hijacked_asns = [asn for asn, r in per_asn.items() if r["hijacked"]]
    all_errored = len(per_asn) > 0 and all(r.get("error") for r in per_asn.values())
    return {
        "per_asn": per_asn,
        "hijacked_asns": hijacked_asns,
        "all_hijacked": len(hijacked_asns) == len(asns) and len(asns) > 0,
        "all_errored": all_errored,
    }


# Registry — adding a new check method is just a new function + entry here.
CHECK_METHODS: dict[str, Callable[[str, str], tuple[str, str | None]]] = {
    "http": check_http,
    "dns": check_dns,
    "hu_consensus": check_hu_consensus,
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
        gs.clear_outage()
        gs.active_mirror = new_mirror
        gs.status = "unknown"
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
    simulated: bool = False,
) -> int | None:
    """
    Send an alert. Prefers posting via the bot to ALERT_CHANNEL_ID (so the
    persistent Ignore / Mirror updated buttons work — Discord rejects
    interactive components on messages from non-application webhooks). If the
    channel ID is unset, falls back to the webhook *without* buttons.
    Returns the message_id so state can track it.
    `simulated=True` prefixes the message so the alert is distinguishable from
    a real incident in the channel; the rest of the dispatch path is unchanged.
    """
    geo_info = GEOS.get(code, {"name": code, "flag": ""})
    emoji = _status_emoji(status)
    level = "DOWN" if status == "red" else "UNCERTAIN"

    header = "🧪 **[TEST]**\n" if simulated else ""
    content = (
        f"{header}"
        f"{emoji} {geo_info['flag']} **{geo_info['name']} {level}**\n"
        f"Mirror: `{mirror}`\n"
        f"Reason: {reason}\n"
        f"First detected: {first_detected.strftime('%Y-%m-%d %H:%M UTC')}"
    )

    if ALERT_CHANNEL_ID:
        try:
            channel = bot.get_channel(int(ALERT_CHANNEL_ID))
            if channel is None:
                channel = await bot.fetch_channel(int(ALERT_CHANNEL_ID))
            msg = await channel.send(content=content, view=AlertView())
            return msg.id
        except Exception as e:
            log.error("Failed to send alert for %s via channel %s: %s",
                      code, ALERT_CHANNEL_ID, e)
            return None

    if not ALERT_WEBHOOK_URL:
        log.warning(
            "Neither DISCORD_ALERT_CHANNEL_ID nor DISCORD_ALERT_WEBHOOK_URL set "
            "— skipping alert for %s", code,
        )
        return None

    try:
        # Webhook fallback — no buttons (Discord rejects components on
        # non-application webhook messages).
        import aiohttp
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(ALERT_WEBHOOK_URL, session=session, client=bot)
            msg = await webhook.send(content=content, wait=True)
            log.warning(
                "Alert for %s sent via webhook fallback — buttons disabled. "
                "Set DISCORD_ALERT_CHANNEL_ID to enable Ignore / Mirror updated.",
                code,
            )
            return msg.id
    except Exception as e:
        log.error("Failed to send alert for %s: %s", code, e)
        return None


# ---------------------------------------------------------------------------
# Shared alert helpers
# ---------------------------------------------------------------------------
def _should_alert(gs: GeoState, prev_status: str, now: datetime) -> bool:
    """Determine if an alert should fire based on state transition and re-alert window."""
    effective = gs.status
    if effective not in ("red", "orange"):
        return False
    if prev_status != effective:
        return True
    if gs.last_alert_sent:
        try:
            last = datetime.fromisoformat(gs.last_alert_sent)
            if (now - last).total_seconds() >= REALERT_AFTER_HOURS * 3600:
                return True
        except ValueError:
            return True
    elif not gs.last_alert_sent:
        return True
    return False


def _realert_due(gs: GeoState, now: datetime) -> bool:
    """True if an alert was already fired and the re-alert window has elapsed."""
    if not gs.alert_fired or not gs.last_alert_sent:
        return False
    try:
        last = datetime.fromisoformat(gs.last_alert_sent)
    except ValueError:
        return True
    return (now - last).total_seconds() >= REALERT_AFTER_HOURS * 3600




# ---------------------------------------------------------------------------
# Monitor cycle
# ---------------------------------------------------------------------------
def _load_redirects() -> dict:
    if REDIRECTS_FILE.exists():
        return json.loads(REDIRECTS_FILE.read_text(encoding="utf-8"))
    return {}


async def run_monitor_cycle(bot: discord.Client) -> None:
    """
    One pass over every enabled GEO. Runs sync check functions in a thread
    executor so the event loop isn't blocked.
    """
    state = MonitorState.load()
    redirects = _load_redirects()
    now = datetime.now(timezone.utc)
    loop = asyncio.get_running_loop()

    for code, geo in GEOS.items():
        if not geo["monitor"]:
            continue
        # GR/PL use scheduled RIPE checks; DK/FR/NO/AE use the daily single-ASN
        # runner. Both are out of band of this 10-minute cycle.
        if geo["check_method"] in ("ripe_reliable_asn", "ripe_daily_single_asn"):
            continue

        gs = state.get(code)

        # Respect ignore window
        if gs.ignored_until:
            try:
                ignored_until = datetime.fromisoformat(gs.ignored_until)
                if ignored_until > now:
                    log.info("%s ignored until %s — skipping", code, gs.ignored_until)
                    continue
                # Ignore window expired — clear it
                gs.ignored_until = None
            except ValueError:
                gs.ignored_until = None

        # Get the mirror from redirects.json
        mirror = (redirects.get(code) or {}).get("mirror")
        if not mirror:
            log.warning("%s: no mirror in redirects.json — skipping", code)
            continue

        # Run the configured check method in a thread
        check_method = geo["check_method"]
        method = CHECK_METHODS.get(check_method)
        if not method:
            log.error("%s: unknown check_method '%s'", code, check_method)
            continue

        try:
            status, reason = await loop.run_in_executor(None, method, code, mirror)
        except Exception as e:
            status, reason = "orange", f"Check raised {type(e).__name__}: {e}"

        prev_status = gs.status
        gs.active_mirror = mirror
        gs.last_checked = now.isoformat(timespec="seconds")
        gs.last_reason = reason
        gs.record(mirror=mirror, status=status, reason=reason, at=now)

        # --- HU consensus path ---
        if check_method == "hu_consensus":
            if status == "up":
                gs.clear_outage()
            elif status == "blocked":
                gs.consecutive_failures += 1
                if gs.consecutive_failures >= FAILURE_THRESHOLD_HU:
                    gs.status = "red"
                    should_alert = _should_alert(gs, prev_status, now)
                    if should_alert:
                        message_id = await _send_alert(
                            bot=bot, code=code, mirror=mirror, status="red",
                            reason=f"All ASNs blocked for {gs.consecutive_failures} consecutive cycles — {reason}",
                            first_detected=now,
                        )
                        gs.last_alert_sent = now.isoformat(timespec="seconds")
                        if message_id is not None:
                            gs.last_alert_message_id = message_id
                else:
                    gs.status = "pending"
                log.info(
                    "%s mirror=%s BLOCKED streak=%d/%d",
                    code, mirror, gs.consecutive_failures, FAILURE_THRESHOLD_HU,
                )
            else:
                # inconclusive — preserve streak, don't count as failure
                log.info("%s mirror=%s inconclusive — streak preserved at %d", code, mirror, gs.consecutive_failures)

        # --- Standard HTTP/DNS path (fallback) ---
        else:
            if status == "up":
                gs.consecutive_failures = 0
                gs.status = "up"
            elif status == "red":
                gs.consecutive_failures += 1
                gs.status = "red"
            else:  # orange
                gs.consecutive_failures += 1
                if gs.consecutive_failures >= PROXY_ERROR_THRESHOLD:
                    gs.status = "orange"
                else:
                    gs.status = prev_status if prev_status in ("up", "red", "orange") else "unknown"

            effective_status = gs.status
            should_alert = _should_alert(gs, prev_status, now)

            if should_alert:
                message_id = await _send_alert(
                    bot=bot, code=code, mirror=mirror, status=effective_status,
                    reason=reason or "(no detail)", first_detected=now,
                )
                gs.last_alert_sent = now.isoformat(timespec="seconds")
                if message_id is not None:
                    gs.last_alert_message_id = message_id

            log.info(
                "%s mirror=%s check=%s effective=%s fails=%d alert=%s reason=%s",
                code, mirror, status, effective_status, gs.consecutive_failures,
                "yes" if should_alert else "no", reason,
            )

    state.save()


# ---------------------------------------------------------------------------
# GR/PL — RIPE reliable-ASN scheduled check
# ---------------------------------------------------------------------------
async def _run_ripe_gr_pl_once(
    bot: discord.Client,
    cc: str,
    label: str,
) -> None:
    """
    One RIPE per-ASN sample for GR or PL. Handles pending-confirmation
    window and new-ASN rapid escalation.
    """
    geo = GEOS.get(cc)
    if not geo or not geo["monitor"]:
        return

    state = MonitorState.load()
    redirects = _load_redirects()
    now = datetime.now(timezone.utc)
    gs = state.get(cc)

    # Respect ignore window
    if gs.ignored_until:
        try:
            if datetime.fromisoformat(gs.ignored_until) > now:
                log.info("%s ignored — skipping RIPE check", cc)
                return
            gs.ignored_until = None
        except ValueError:
            gs.ignored_until = None

    mirror = (redirects.get(cc) or {}).get("mirror")
    if not mirror:
        log.warning("%s: no mirror in redirects.json — skipping RIPE check", cc)
        return

    if not RIPE_ATLAS_API_KEY:
        log.warning("%s: RIPE_ATLAS_API_KEY not set — skipping", cc)
        return

    # Build ASN list: reliable ASN + peer ASNs from config
    reliable = RELIABLE_ASN.get(cc)
    peer_asns = [a for a in geo.get("asns", []) if a != reliable]
    all_asns = ([reliable] if reliable else []) + peer_asns
    if not all_asns:
        log.warning("%s: no ASNs configured — skipping RIPE check", cc)
        return

    log.info("[%s] RIPE per-ASN check (%s) — ASNs %s", cc, label, all_asns)

    loop = asyncio.get_running_loop()
    summary = await loop.run_in_executor(
        None, ripe_check_per_asn, mirror, cc, all_asns
    )

    # No data — every measurement errored (e.g. RIPE credits exhausted).
    # Do NOT mutate state: an empty hijacked_asns list with all-errors must
    # never silently clear a real pending-confirmation window.
    if summary.get("all_errored"):
        first_err = next(
            (r.get("error") for r in summary["per_asn"].values() if r.get("error")),
            "unknown",
        )
        log.warning("[%s] ripe_unavailable reason=%s — preserving state", cc, first_err)
        return

    hijacked_asns = summary["hijacked_asns"]
    reliable_hijacked = reliable and reliable in hijacked_asns

    gs.active_mirror = mirror
    gs.last_checked = now.isoformat(timespec="seconds")
    gs.record(mirror=mirror, status="hijacked" if hijacked_asns else "up",
              reason=f"RIPE {label}: hijacked ASNs={hijacked_asns}", at=now)

    # --- Clean result: no ASNs hijacked ---
    if not hijacked_asns and not reliable_hijacked:
        gs.clear_outage()
        gs.last_reason = None
        log.info("[%s] %s — UP (no ASNs hijacked)", cc, mirror)
        state.save()
        return

    # --- Down candidate ---
    prev_blocked = set(gs.last_blocked_asns or [])
    now_blocked = set(hijacked_asns)
    new_asns = sorted(now_blocked - prev_blocked)
    gs.last_blocked_asns = sorted(now_blocked)
    gs.last_reason = f"Hijacked ASNs: {', '.join(f'AS{a}' for a in hijacked_asns)}"

    # Rapid escalation: new ASN hijacked mid-pending-window → alert immediately
    # (also re-fires if a new ASN appears after the 4h re-alert window has elapsed)
    if gs.pending_confirmation and new_asns and (not gs.alert_fired or _realert_due(gs, now)):
        log.warning("[%s] RAPID ESCALATION — new ASNs hijacked: %s", cc, new_asns)
        gs.status = "red"
        gs.alert_fired = True
        message_id = await _send_alert(
            bot=bot, code=cc, mirror=mirror, status="red",
            reason=f"DNS hijack expanded — new ASNs: {', '.join(f'AS{a}' for a in new_asns)}",
            first_detected=now,
        )
        gs.last_alert_sent = now.isoformat(timespec="seconds")
        if message_id is not None:
            gs.last_alert_message_id = message_id
        state.save()
        return

    # Not yet in pending window → open one
    if not gs.pending_confirmation:
        gs.pending_confirmation = True
        gs.pending_first_seen = now.isoformat(timespec="seconds")
        gs.pending_attempts = 1
        gs.status = "pending"
        state.save()
        log.warning(
            "[%s] Entering pending-confirmation (attempt 1/%d) — "
            "reliable AS%s %s, %d ASN(s) hijacked. Retry in %d min.",
            cc, PENDING_MAX_ATTEMPTS,
            reliable, "HIJACKED" if reliable_hijacked else "clean",
            len(hijacked_asns), PENDING_RETRY_MINUTES,
        )
        return

    # Already pending → this is a retry
    gs.pending_attempts += 1
    gs.status = "pending"
    log.warning(
        "[%s] Pending retry %d/%d — %d ASN(s) still hijacked",
        cc, gs.pending_attempts, PENDING_MAX_ATTEMPTS, len(hijacked_asns),
    )

    if gs.pending_attempts >= PENDING_MAX_ATTEMPTS and (
        not gs.alert_fired or _realert_due(gs, now)
    ):
        gs.status = "red"
        gs.alert_fired = True
        message_id = await _send_alert(
            bot=bot, code=cc, mirror=mirror, status="red",
            reason=(
                f"DNS hijack confirmed after {PENDING_MAX_ATTEMPTS} checks — "
                f"reliable AS{reliable}={'HIJACKED' if reliable_hijacked else 'clean'}, "
                f"{len(hijacked_asns)} ASN(s) hijacked"
            ),
            first_detected=datetime.fromisoformat(gs.pending_first_seen) if gs.pending_first_seen else now,
        )
        gs.last_alert_sent = now.isoformat(timespec="seconds")
        if message_id is not None:
            gs.last_alert_message_id = message_id

    state.save()


async def run_ripe_scheduled(bot: discord.Client) -> None:
    """Run the scheduled RIPE check for all GR/PL GEOs with monitor: true."""
    for cc in ("GR", "PL"):
        geo = GEOS.get(cc)
        if not geo or not geo["monitor"]:
            continue
        try:
            await _run_ripe_gr_pl_once(bot, cc, label="scheduled")
        except Exception as e:
            log.error("[%s] Scheduled RIPE check failed: %s", cc, e, exc_info=True)


async def tick_pending_retries(bot: discord.Client) -> None:
    """
    Check if any GR/PL geo has a pending-confirmation window with a retry
    due (>= PENDING_RETRY_MINUTES since last check). If so, run another
    RIPE check for that geo.
    """
    now = datetime.now(timezone.utc)
    state = MonitorState.load()

    for cc in ("GR", "PL"):
        geo = GEOS.get(cc)
        if not geo or not geo["monitor"]:
            continue
        gs = state.get(cc)
        if not gs.pending_confirmation or gs.alert_fired:
            continue
        if gs.pending_attempts >= PENDING_MAX_ATTEMPTS:
            continue
        if not gs.last_checked:
            continue
        try:
            last = datetime.fromisoformat(gs.last_checked)
        except ValueError:
            continue
        if (now - last) < timedelta(minutes=PENDING_RETRY_MINUTES):
            continue

        log.info("[%s] Pending retry due — running RIPE check", cc)
        try:
            await _run_ripe_gr_pl_once(bot, cc, label="pending retry")
        except Exception as e:
            log.error("[%s] Pending retry failed: %s", cc, e, exc_info=True)


# ---------------------------------------------------------------------------
# DK/FR/NO/AE — daily single-ASN RIPE check (Tier 3, runs once at 04 UTC)
# ---------------------------------------------------------------------------
async def run_daily_single_asn_check(bot: discord.Client) -> None:
    """
    Iterate every geo with check_method == 'ripe_daily_single_asn' and
    monitor: true. Fire exactly one RIPE measurement against the geo's
    reliable ASN. Alert on hijack; clear outage on clean. No state mutation
    when the measurement errors (e.g. low credit).
    """
    state = MonitorState.load()
    redirects = _load_redirects()
    now = datetime.now(timezone.utc)
    loop = asyncio.get_running_loop()

    for code, geo in GEOS.items():
        if not geo["monitor"] or geo["check_method"] != "ripe_daily_single_asn":
            continue

        gs = state.get(code)

        # Respect ignore window
        if gs.ignored_until:
            try:
                if datetime.fromisoformat(gs.ignored_until) > now:
                    log.info("[%s] ignored — skipping daily RIPE check", code)
                    continue
                gs.ignored_until = None
            except ValueError:
                gs.ignored_until = None

        mirror = (redirects.get(code) or {}).get("mirror")
        if not mirror:
            log.warning("[%s] No mirror in redirects.json — skipping daily check", code)
            continue

        asn = RELIABLE_ASN.get(code)
        if not asn:
            log.error("[%s] No RELIABLE_ASN configured — skipping daily check", code)
            continue

        log.info("[%s] Daily single-ASN RIPE check — AS%s @ %s", code, asn, mirror)
        try:
            result = await loop.run_in_executor(
                None, ripe_check_asn, mirror, code, asn,
            )
        except Exception as e:
            log.error("[%s] Daily check raised %s: %s", code, type(e).__name__, e)
            continue

        # No data — measurement errored (low credit, API failure). Do not
        # mutate state: must not flip a real outage to "clean".
        if result.get("error"):
            log.warning("[%s] ripe_unavailable cc=%s reason=%s — preserving state",
                        code, code, result["error"])
            continue

        gs.active_mirror = mirror
        gs.last_checked = now.isoformat(timespec="seconds")
        gs.record(
            mirror=mirror,
            status="hijacked" if result["hijacked"] else "up",
            reason=f"Daily single-ASN: AS{asn} ips={result['ips']}",
            at=now,
        )

        if result["hijacked"]:
            ips = ", ".join(result["ips"]) or "no IPs"
            reason = (
                f"Daily RIPE single-ASN check: AS{asn} hijacked, IPs={ips}"
            )
            gs.last_reason = reason
            prev_status = gs.status
            gs.status = "red"

            # Fire on first detection, or on the 4h re-alert window.
            if not gs.alert_fired or _realert_due(gs, now):
                gs.alert_fired = True
                message_id = await _send_alert(
                    bot=bot, code=code, mirror=mirror, status="red",
                    reason=reason, first_detected=now,
                )
                gs.last_alert_sent = now.isoformat(timespec="seconds")
                if message_id is not None:
                    gs.last_alert_message_id = message_id
                log.warning("[%s] Daily check ALERT fired (prev_status=%s)",
                            code, prev_status)
            else:
                log.info("[%s] Daily check still hijacked — alert suppressed (within re-alert window)",
                         code)
        else:
            # Clean — silently clear outage state if it was set.
            if gs.status == "red" or gs.alert_fired:
                log.info("[%s] Daily check clean — clearing outage state", code)
            gs.clear_outage()
            gs.last_reason = None

    state.save()


def migrate_legacy_decodo_state() -> None:
    """
    One-shot startup cleanup: any geo now using ripe_daily_single_asn that
    still carries Decodo-era streak/alert state from the old check method
    gets a clean slate. The daily runner doesn't read those fields, so
    leftover values would just be misleading in monitor_state.json.
    """
    state = MonitorState.load()
    changed = False
    for code, geo in GEOS.items():
        if geo.get("check_method") != "ripe_daily_single_asn":
            continue
        gs = state.get(code)
        if gs.consecutive_failures > 0 or gs.alert_fired:
            log.info("[%s] migrating legacy Decodo state — clearing outage flags", code)
            gs.clear_outage()
            changed = True
    if changed:
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
# Test alert dispatch — used by /mirror-test when the real check returns red.
# Reuses the live _send_alert + MonitorState write path so the alert is
# end-to-end identical to a real one, just labelled [TEST]. State writes go
# under a sentinel "SIM" code so per-country state is never touched.
# ---------------------------------------------------------------------------
SIM_CODE = "SIM"


async def dispatch_test_alert(
    bot: discord.Client,
    geo_code: str,
    mirror: str,
    status: str,
    reason: str,
) -> tuple[bool, str]:
    """
    Send a [TEST]-labelled alert through the same _send_alert + MonitorState
    write path the live monitor uses. The alert renders with the chosen geo's
    flag/name so it reads like a real incident; state goes under SIM_CODE so
    real per-country state is untouched. Returns (ok, summary) for the caller.
    """
    geo_code = geo_code.upper()
    if geo_code not in GEOS:
        return False, f"Unknown GEO `{geo_code}`."
    if not ALERT_CHANNEL_ID and not ALERT_WEBHOOK_URL:
        return False, (
            "Neither DISCORD_ALERT_CHANNEL_ID nor DISCORD_ALERT_WEBHOOK_URL is set."
        )

    now = datetime.now(timezone.utc)
    log.warning("test-alert sim=true geo=%s target=%s status=%s reason=%s",
                geo_code, mirror, status, reason)

    state = MonitorState.load()
    gs = state.get(SIM_CODE)
    gs.active_mirror = mirror
    gs.status = status
    gs.consecutive_failures = (gs.consecutive_failures or 0) + 1
    gs.last_checked = now.isoformat(timespec="seconds")
    gs.last_reason = reason
    gs.alert_fired = True
    gs.record(mirror=mirror, status=status, reason=reason, at=now)

    message_id = await _send_alert(
        bot=bot,
        code=geo_code,
        mirror=mirror,
        status=status,
        reason=reason,
        first_detected=now,
        simulated=True,
    )
    gs.last_alert_sent = now.isoformat(timespec="seconds")
    if message_id is not None:
        gs.last_alert_message_id = message_id
    state.save()

    log.info("test-alert sim=true dispatched message_id=%s", message_id)

    if message_id is None:
        return False, "Alert dispatch failed — check the bot log."
    return True, f"[TEST] alert posted (id {message_id})."


# ---------------------------------------------------------------------------
# Background task wiring
# ---------------------------------------------------------------------------
def create_monitor_task(bot: discord.Client) -> tasks.Loop:
    """
    Create a discord.ext.tasks loop that runs the monitor cycle on the
    configured interval. Also handles:
      - GR/PL scheduled RIPE checks at 04:00 and 16:00 UTC
      - GR/PL pending-confirmation retry ticks
      - DK/FR/NO/AE daily single-ASN RIPE check at DAILY_RIPE_HOUR UTC
    Caller is responsible for .start()ing it after the bot is ready.
    """
    # One-shot cleanup of leftover Decodo-era state for any geo now on
    # ripe_daily_single_asn. Cheap, safe to run on every startup.
    try:
        migrate_legacy_decodo_state()
    except Exception as e:
        log.error("Legacy state migration failed: %s", e, exc_info=True)

    # Track the last hour each scheduled job ran to avoid double-firing.
    _last_ripe_hour: dict[str, int | None] = {"value": None}
    _last_daily_ripe_date: dict[str, str | None] = {"value": None}

    @tasks.loop(minutes=INTERVAL_MINUTES)
    async def monitor_loop():
        now = datetime.now(timezone.utc)

        # --- Regular HTTP cycle (HU only — DK/NO/FR/AE moved to daily) ---
        try:
            await run_monitor_cycle(bot)
        except Exception as e:
            print(f"[monitor] Unexpected error in cycle: {type(e).__name__}: {e}", flush=True)

        # --- GR/PL scheduled RIPE checks at 04:00 and 16:00 UTC ---
        current_hour = now.hour
        if current_hour in RIPE_SCHEDULE_HOURS and _last_ripe_hour["value"] != current_hour:
            _last_ripe_hour["value"] = current_hour
            try:
                await run_ripe_scheduled(bot)
            except Exception as e:
                print(f"[monitor] RIPE scheduled check error: {type(e).__name__}: {e}", flush=True)

        # Reset tracker when we leave a scheduled hour
        if current_hour not in RIPE_SCHEDULE_HOURS:
            _last_ripe_hour["value"] = None

        # --- DK/FR/NO/AE daily single-ASN RIPE check at DAILY_RIPE_HOUR UTC ---
        today_str = now.strftime("%Y-%m-%d")
        if current_hour == DAILY_RIPE_HOUR and _last_daily_ripe_date["value"] != today_str:
            _last_daily_ripe_date["value"] = today_str
            try:
                await run_daily_single_asn_check(bot)
            except Exception as e:
                print(f"[monitor] Daily RIPE check error: {type(e).__name__}: {e}", flush=True)

        # --- GR/PL pending-confirmation retry ticks ---
        try:
            await tick_pending_retries(bot)
        except Exception as e:
            print(f"[monitor] Pending retry tick error: {type(e).__name__}: {e}", flush=True)

    @monitor_loop.before_loop
    async def wait_until_ready():
        await bot.wait_until_ready()

    return monitor_loop
