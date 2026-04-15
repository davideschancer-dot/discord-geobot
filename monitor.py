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
    }

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
PROXY_HOST = os.getenv("PROXY_HOST", "gate.decodo.com")
PROXY_PORT = os.getenv("PROXY_PORT", "10001")
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

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
    """
    DNS check via RIPE Atlas probes — not yet implemented.
    Poland phase will implement this: detects ISP DNS hijack by comparing
    resolved IP to the real Cloudflare IP 104.24.14.93.
    """
    return "orange", "DNS check method not yet implemented for this GEO"


# Registry — adding a new check method is just a new function + entry here.
CHECK_METHODS: dict[str, Callable[[str, str], tuple[str, str | None]]] = {
    "http": check_http,
    "dns": check_dns,
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
        method = CHECK_METHODS.get(geo["check_method"])
        if not method:
            log.error("%s: unknown check_method '%s'", code, geo["check_method"])
            continue

        try:
            status, reason = await loop.run_in_executor(None, method, code, mirror)
        except Exception as e:
            status, reason = "orange", f"Check raised {type(e).__name__}: {e}"

        # ORANGE only fires after PROXY_ERROR_THRESHOLD consecutive proxy errors.
        # Translate an immediate proxy/orange result into a counter bump; only
        # surface it once the threshold is crossed.
        prev_status = gs.status
        prev_consecutive = gs.consecutive_failures

        if status == "up":
            effective_status = "up"
            gs.consecutive_failures = 0
        elif status == "red":
            effective_status = "red"
            gs.consecutive_failures += 1
        else:  # orange
            gs.consecutive_failures += 1
            if gs.consecutive_failures >= PROXY_ERROR_THRESHOLD:
                effective_status = "orange"
            else:
                # Below threshold — don't alert yet, keep previous state
                effective_status = prev_status if prev_status in ("up", "red", "orange") else "unknown"

        gs.status = effective_status
        gs.active_mirror = mirror
        gs.last_checked = now.isoformat(timespec="seconds")
        gs.last_reason = reason
        # Record the raw check result in per-GEO history (capped).
        gs.record(mirror=mirror, status=status, reason=reason, at=now)

        # Decide whether to fire an alert
        should_alert = False
        if effective_status in ("red", "orange"):
            if prev_status != effective_status:
                # Newly degraded
                should_alert = True
            elif gs.last_alert_sent:
                # Still degraded — re-alert after configured window
                try:
                    last = datetime.fromisoformat(gs.last_alert_sent)
                    if (now - last).total_seconds() >= REALERT_AFTER_HOURS * 3600:
                        should_alert = True
                except ValueError:
                    should_alert = True
            else:
                # Degraded but no alert has ever been sent for this incident
                should_alert = True

        if should_alert:
            message_id = await _send_alert(
                bot=bot,
                code=code,
                mirror=mirror,
                status=effective_status,
                reason=reason or "(no detail)",
                first_detected=now,
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
