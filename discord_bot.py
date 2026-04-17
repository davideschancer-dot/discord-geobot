"""
Discord Bot — GEO Redirect Monitor
------------------------------------
Slash commands:
  /check-redirect geo:HU  — Check which mirror chancer.bet redirects to
                             via EC2 + NordVPN tunnel.
  /redirect-status         — Show all known URL redirects.

The redirect check calls an EC2 instance (eu-central-1) that routes
chancer.bet traffic through a NordVPN OpenVPN tunnel.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import discord
import requests
import yaml
from discord import app_commands
from dotenv import load_dotenv

import monitor

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# EC2 redirect checker endpoint (NordVPN Hungary tunnel)
REDIRECT_CHECKER_URL = os.getenv(
    "REDIRECT_CHECKER_URL",
    "http://63.178.175.200:8080",
)
REDIRECT_CHECKER_KEY = os.getenv("REDIRECT_CHECKER_KEY", "chancer-geo-2026")

PROJECT_DIR = Path(__file__).resolve().parent
REDIRECTS_FILE = PROJECT_DIR / "redirects.json"
CONFIG_FILE = PROJECT_DIR / "config.yaml"

TARGET_DOMAIN = "chancer.bet"

# Load GEO definitions from config.yaml so adding a country is config-only.
_raw_cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
GEO_MAP: dict[str, dict] = {}
for geo in _raw_cfg.get("geos", []):
    GEO_MAP[geo["code"]] = {
        "name": geo["name"],
        "flag": geo.get("flag", ""),
    }


# ---------------------------------------------------------------------------
# Redirects JSON helpers
# ---------------------------------------------------------------------------
def load_redirects() -> dict:
    if REDIRECTS_FILE.exists():
        return json.loads(REDIRECTS_FILE.read_text(encoding="utf-8"))
    return {}


def save_redirects(data: dict) -> None:
    REDIRECTS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# EC2 redirect checker
# ---------------------------------------------------------------------------
def resolve_mirror_sync(country_code: str) -> tuple[str | None, str | None]:
    """
    Call the EC2 redirect checker endpoint.
    The EC2 instance routes chancer.bet through NordVPN Hungary.
    Returns (mirror_host, error_message).
    """
    try:
        resp = requests.get(
            f"{REDIRECT_CHECKER_URL}/check",
            params={"key": REDIRECT_CHECKER_KEY, "geo": country_code.lower()},
            timeout=60,
        )
        data = resp.json()

        if resp.status_code == 200 and "mirror" in data:
            return data["mirror"], None

        return None, data.get("error", f"HTTP {resp.status_code}")

    except requests.exceptions.Timeout:
        return None, "Timeout calling redirect checker"
    except requests.exceptions.ConnectionError:
        return None, "Cannot reach redirect checker (EC2 down?)"
    except Exception as e:
        return None, f"Error: {e}"


# ---------------------------------------------------------------------------
# Discord bot setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

GEO_CHOICES = [
    app_commands.Choice(name=f"{info['flag']} {info['name']} ({code})", value=code)
    for code, info in GEO_MAP.items()
]

# Auto-delete ephemeral replies after this many seconds so the user's DMs
# / ephemeral stack doesn't accumulate. Discord itself never expires them.
EPHEMERAL_TTL_SECONDS = 60  # 1 minute

# Monitor background task handle (set in on_ready)
_monitor_task = None


def _cleanup_ephemeral(interaction: discord.Interaction, delay: int = EPHEMERAL_TTL_SECONDS):
    """Schedule deletion of this interaction's original ephemeral response."""
    async def _do_delete():
        await asyncio.sleep(delay)
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException, AttributeError):
            # Message was already dismissed/deleted or interaction expired — fine.
            pass

    asyncio.create_task(_do_delete())


MONITOR_PAUSE_SECONDS = 60  # how long to wait after check before restarting monitor


async def _stop_monitor():
    """Stop the monitor loop if it's running."""
    global _monitor_task
    if _monitor_task is not None and _monitor_task.is_running():
        _monitor_task.cancel()
        print("[pause-on-check] Monitor stopped", flush=True)


async def _restart_monitor_after_delay(delay: int = MONITOR_PAUSE_SECONDS):
    """Wait, then restart the monitor loop."""
    global _monitor_task
    await asyncio.sleep(delay)
    if _monitor_task is not None and not _monitor_task.is_running():
        _monitor_task.start()
        print("[pause-on-check] Monitor restarted", flush=True)


async def run_check_and_reply(
    interaction: discord.Interaction, geo: str, geo_info: dict
):
    """Run the redirect check and post the result as a public channel message.
    Pauses the monitor loop during the check to prevent ticking mid-update."""
    try:
        # 1. Stop the monitor
        await _stop_monitor()

        # 2. Run the EC2 NordVPN checker
        loop = asyncio.get_running_loop()
        mirror, err = await loop.run_in_executor(None, resolve_mirror_sync, geo)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        if err:
            await interaction.channel.send(
                f"{geo_info['flag']} **{geo_info['name']}** — Failed to resolve mirror.\n`{err}`"
            )
            # Still restart monitor even on failure
            asyncio.create_task(_restart_monitor_after_delay())
            return

        # 3. Write result to redirects.json
        redirects = load_redirects()
        redirects[geo] = {
            "mirror": mirror,
            "updated": now,
            "method": "ec2_vpn",
        }
        save_redirects(redirects)

        await interaction.channel.send(
            f"{geo_info['flag']} **{geo_info['name']}** → `{mirror}` (ec2_vpn, {now[:16].replace('T', ' ')})"
        )

        # 4. Wait 60s then restart the monitor
        asyncio.create_task(_restart_monitor_after_delay())

    except Exception as e:
        await interaction.channel.send(f"Error: `{e}`")
        asyncio.create_task(_restart_monitor_after_delay())


class PurgeConfirmView(discord.ui.View):
    """Ephemeral Yes/No prompt asking whether to purge channel history."""

    def __init__(self, geo: str, geo_info: dict):
        super().__init__(timeout=60)
        self.geo = geo
        self.geo_info = geo_info

    @discord.ui.button(label="Yes, delete history", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Disable buttons on the prompt
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Purging history and checking {self.geo_info['name']}...",
            view=self,
        )

        # Purge channel history (bot's own prompt is ephemeral — not affected)
        deleted_count = 0
        purge_error = None
        try:
            deleted = await interaction.channel.purge(
                limit=100,
                check=lambda m: True,
                bulk=True,
                reason="check-redirect requested history clear",
            )
            deleted_count = len(deleted)
            print(f"[purge] Deleted {deleted_count} messages", flush=True)
        except discord.Forbidden as e:
            purge_error = "bot lacks Manage Messages permission"
            print(f"[purge] Forbidden: {e}", flush=True)
        except (discord.NotFound, discord.HTTPException, AttributeError) as e:
            purge_error = str(e)
            print(f"[purge] {type(e).__name__}: {e}", flush=True)

        # Let the user know the outcome in the ephemeral message
        if purge_error:
            await interaction.edit_original_response(
                content=f"Could not purge: {purge_error}. Checking {self.geo_info['name']} anyway..."
            )
        else:
            await interaction.edit_original_response(
                content=f"Deleted {deleted_count} messages. Checking {self.geo_info['name']}..."
            )

        await run_check_and_reply(interaction, self.geo, self.geo_info)
        self.stop()

    @discord.ui.button(label="No, keep history", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Checking {self.geo_info['name']} without purging...",
            view=self,
        )
        await run_check_and_reply(interaction, self.geo, self.geo_info)
        self.stop()


# ---------------------------------------------------------------------------
# /check-redirect
# ---------------------------------------------------------------------------
@tree.command(name="check-redirect", description="Check which mirror chancer.bet redirects to for a GEO")
@app_commands.describe(geo="Country code (e.g. HU)")
@app_commands.choices(geo=GEO_CHOICES)
async def check_redirect(interaction: discord.Interaction, geo: str):
    geo = geo.upper()
    geo_info = GEO_MAP.get(geo)
    if not geo_info:
        await interaction.response.send_message(
            f"Unknown GEO: `{geo}`. Available: {', '.join(GEO_MAP.keys())}",
            ephemeral=True,
        )
        _cleanup_ephemeral(interaction)
        return

    view = PurgeConfirmView(geo, geo_info)
    await interaction.response.send_message(
        f"Delete previous messages in this channel before checking **{geo_info['name']}**?",
        view=view,
        ephemeral=True,
    )
    _cleanup_ephemeral(interaction)


# ---------------------------------------------------------------------------
# /redirect-status
# ---------------------------------------------------------------------------
@tree.command(name="redirect-status", description="Show all known URL redirects")
async def redirect_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data = load_redirects()
    if not data:
        await interaction.followup.send(
            "No redirects saved yet. Run `/check-redirect` first.",
            ephemeral=True,
        )
        _cleanup_ephemeral(interaction)
        return

    lines = []
    for code, entry in sorted(data.items()):
        geo_info = GEO_MAP.get(code, {"flag": "", "name": code})
        mirror = entry.get("mirror", "?")
        method = entry.get("method", "?")
        updated = entry.get("updated", "?")
        if "T" in updated:
            updated = updated.replace("T", " ")[:16]
        lines.append(f"{geo_info['flag']} {geo_info['name']} → `{mirror}` ({method}, {updated})")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# /mirror-test — simulation/demo command for stakeholders
# ---------------------------------------------------------------------------
def _clean_domain(url: str) -> str:
    """Strip protocol, path, and trailing slashes from a user-supplied URL."""
    url = url.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/")


def _get_ripe_asns(geo_code: str) -> list[str]:
    """Get the ASN list to use for RIPE checks for a given GEO."""
    if geo_code in monitor.CONFIRM_ASNS:
        return monitor.CONFIRM_ASNS[geo_code]
    geo = monitor.GEOS.get(geo_code, {})
    return geo.get("asns", [])


@tree.command(name="mirror-test", description="Test if a domain is blocked in a country (simulation — does not affect monitoring)")
@app_commands.describe(
    url="Domain to test (e.g. wolfycasino.com)",
    geo="Country to test from",
)
@app_commands.choices(geo=GEO_CHOICES)
async def mirror_test(interaction: discord.Interaction, url: str, geo: str):
    geo = geo.upper()
    geo_info = GEO_MAP.get(geo)
    if not geo_info:
        await interaction.response.send_message(
            f"Unknown GEO: `{geo}`.", ephemeral=True,
        )
        return

    domain = _clean_domain(url)
    await interaction.response.defer()

    loop = asyncio.get_running_loop()

    # Run Decodo HTTP and RIPE DNS checks concurrently
    if geo == "HU":
        http_fut = loop.run_in_executor(None, monitor.check_hu_consensus, geo, domain)
    else:
        http_fut = loop.run_in_executor(None, monitor.check_http, geo, domain)

    asns = _get_ripe_asns(geo)
    ripe_fut = None
    if asns and monitor.RIPE_ATLAS_API_KEY:
        ripe_fut = loop.run_in_executor(None, monitor.ripe_check_per_asn, domain, geo, asns)

    http_status, http_reason = await http_fut

    # For HU consensus, map "blocked" → "red" and "inconclusive" → "orange"
    if http_status == "blocked":
        http_status = "red"
    elif http_status == "inconclusive":
        http_status = "orange"

    ripe_summary = None
    ripe_failed = False
    if ripe_fut is not None:
        try:
            ripe_summary = await ripe_fut
        except Exception as e:
            ripe_failed = True
            print(f"[mirror-test] RIPE check failed: {e}", flush=True)

    # --- Traffic light verdict ---
    hijacked_count = 0
    total_asns = len(asns)

    if ripe_summary and not ripe_failed:
        hijacked_count = len(ripe_summary.get("hijacked_asns", []))

        if total_asns > 0 and hijacked_count >= total_asns / 2:
            verdict = "red"
        elif hijacked_count > 0:
            verdict = "orange"
        elif http_status == "red" or http_status == "orange":
            verdict = "orange"
        else:
            verdict = "green"
    else:
        # RIPE unavailable — Decodo only
        if http_status == "up":
            verdict = "green"
        elif http_status == "orange":
            verdict = "orange"
        else:
            verdict = "red"

    # --- Build embed ---
    colors = {"green": 0x2ECC71, "orange": 0xF39C12, "red": 0xE74C3C}
    titles = {
        "green": "Available",
        "orange": "Potentially Blocked",
        "red": "Blocked",
    }
    emojis = {"green": "\U0001f7e2", "orange": "\U0001f7e0", "red": "\U0001f534"}

    verdict_label = titles[verdict]
    summary_map = {
        "green": f"`{domain}` appears fully accessible in {geo_info['name']}.",
        "orange": f"`{domain}` shows signs of issues in {geo_info['name']}, but not enough evidence to confirm a block.",
        "red": f"`{domain}` is blocked in {geo_info['name']} with high confidence.",
    }

    embed = discord.Embed(
        title=f"{emojis[verdict]} {verdict_label}",
        description=summary_map[verdict],
        color=colors[verdict],
    )
    embed.add_field(name="Domain", value=f"`{domain}`", inline=True)
    embed.add_field(name="Country", value=f"{geo_info['flag']} {geo_info['name']}", inline=True)
    embed.add_field(
        name="HTTP Check",
        value=f"**{http_status.upper()}**{(' — ' + http_reason) if http_reason else ''}",
        inline=False,
    )

    # DNS check field
    if ripe_failed or (ripe_fut is None):
        dns_text = "DNS confirmation unavailable"
        if not monitor.RIPE_ATLAS_API_KEY:
            dns_text += " (RIPE API key not set)"
        elif not asns:
            dns_text += " (no ASNs configured for this GEO)"
        elif ripe_failed:
            dns_text += " (API error)"
    elif ripe_summary:
        hijacked_asns = ripe_summary.get("hijacked_asns", [])
        per_asn = ripe_summary.get("per_asn", {})
        lines = [f"**{hijacked_count}/{total_asns}** ASNs hijacked"]
        for asn, data in per_asn.items():
            ips_str = ", ".join(data.get("ips", [])) or "no response"
            status_icon = "\U0001f534" if data.get("hijacked") else "\u2705"
            lines.append(f"{status_icon} AS{asn}: {ips_str}")
        dns_text = "\n".join(lines)
    else:
        dns_text = "No RIPE data"

    embed.add_field(name="DNS Check", value=dns_text, inline=False)
    embed.set_footer(text="Simulation only \u2014 does not affect live monitoring")

    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    global _monitor_task

    # Register persistent AlertView so buttons survive bot restarts
    bot.add_view(monitor.AlertView())

    synced = await tree.sync()
    print(f"Bot ready — logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"Synced {len(synced)} commands: {[c.name for c in synced]}", flush=True)
    print(f"GEOs loaded: {', '.join(GEO_MAP.keys())}", flush=True)

    # Start the background monitor loop
    if _monitor_task is None:
        _monitor_task = monitor.create_monitor_task(bot)
        _monitor_task.start()
        print("Monitor task started", flush=True)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        raise SystemExit(1)
    bot.run(DISCORD_BOT_TOKEN)
