"""
Discord Bot — GEO Redirect Monitor
------------------------------------
Slash commands:
  /check-redirect geo:HU  — Check which mirror chancer.bet redirects to
                             via EC2 + NordVPN Hungary tunnel.
  /redirects               — Show current redirect table from redirects.json.
  /set-redirect geo:HU mirror:chancer8.xyz — Manually set a redirect entry.

The redirect check calls an EC2 instance (eu-central-1) that routes
chancer.bet traffic through a NordVPN Hungary OpenVPN tunnel.
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

import monitor  # background GEO health monitor (phase 2)

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


async def run_check_and_reply(
    interaction: discord.Interaction, geo: str, geo_info: dict
):
    """Run the redirect check and post the result as a public channel message."""
    try:
        loop = asyncio.get_running_loop()
        mirror, err = await loop.run_in_executor(None, resolve_mirror_sync, geo)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        if err:
            await interaction.channel.send(
                f"{geo_info['flag']} **{geo_info['name']}** — Failed to resolve mirror.\n`{err}`"
            )
            return

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
    except Exception as e:
        await interaction.channel.send(f"Error: `{e}`")


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
                content=f"⚠️ Could not purge: {purge_error}. Checking {self.geo_info['name']} anyway..."
            )
        else:
            await interaction.edit_original_response(
                content=f"🗑️ Deleted {deleted_count} messages. Checking {self.geo_info['name']}..."
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
        return

    view = PurgeConfirmView(geo, geo_info)
    await interaction.response.send_message(
        f"Delete previous messages in this channel before checking **{geo_info['name']}**?",
        view=view,
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /redirects
# ---------------------------------------------------------------------------
@tree.command(name="redirects", description="Show current GEO redirect table")
async def redirects(interaction: discord.Interaction):
    data = load_redirects()
    if not data:
        await interaction.response.send_message(
            "No redirects saved yet. Run `/check-redirect` first.",
            ephemeral=True,
        )
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

    await interaction.response.send_message("\n".join(lines))


# ---------------------------------------------------------------------------
# /redirect-table
# ---------------------------------------------------------------------------
@tree.command(name="redirect-table", description="Show current redirects as a table")
async def redirect_table(interaction: discord.Interaction):
    data = load_redirects()
    if not data:
        await interaction.response.send_message(
            "No redirects saved yet. Run `/check-redirect` first.",
            ephemeral=True,
        )
        return

    # Build a fixed-width table
    col1_header = "Country"
    col2_header = "Mirror"
    rows = []
    for code, entry in sorted(data.items()):
        geo_info = GEO_MAP.get(code, {"name": code})
        rows.append((geo_info["name"], entry.get("mirror", "?")))

    col1_width = max(len(col1_header), *(len(r[0]) for r in rows))
    col2_width = max(len(col2_header), *(len(r[1]) for r in rows))

    separator = f"+{'-' * (col1_width + 2)}+{'-' * (col2_width + 2)}+"
    header = f"| {col1_header:<{col1_width}} | {col2_header:<{col2_width}} |"
    body_lines = [
        f"| {name:<{col1_width}} | {mirror:<{col2_width}} |"
        for name, mirror in rows
    ]

    table = "\n".join([separator, header, separator, *body_lines, separator])
    await interaction.response.send_message(f"```\n{table}\n```")


# ---------------------------------------------------------------------------
# /set-redirect
# ---------------------------------------------------------------------------
@tree.command(name="set-redirect", description="Manually set the redirect mirror for a GEO")
@app_commands.describe(geo="Country code (e.g. HU)", mirror="Mirror domain (e.g. chancer8.xyz)")
@app_commands.choices(geo=GEO_CHOICES)
async def set_redirect(interaction: discord.Interaction, geo: str, mirror: str):
    geo = geo.upper()
    geo_info = GEO_MAP.get(geo)
    if not geo_info:
        await interaction.response.send_message(
            f"Unknown GEO: `{geo}`. Available: {', '.join(GEO_MAP.keys())}",
            ephemeral=True,
        )
        return

    mirror = mirror.lower().strip()
    if "://" in mirror:
        mirror = urllib.parse.urlparse(mirror).netloc or mirror
    mirror = mirror.rstrip("/")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    redirects_data = load_redirects()
    redirects_data[geo] = {
        "mirror": mirror,
        "updated": now,
        "method": "manual",
    }
    save_redirects(redirects_data)

    await interaction.response.send_message(
        f"{geo_info['flag']} **{geo_info['name']}** → `{mirror}` (manual)\nSaved to `redirects.json`."
    )


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------
# Module-level handle so we only create the monitor task once even if
# on_ready fires again after a reconnect.
_monitor_task = None


@bot.event
async def on_ready():
    global _monitor_task

    synced = await tree.sync()
    print(f"Bot ready — logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"Synced {len(synced)} commands: {[c.name for c in synced]}", flush=True)
    print(f"GEOs loaded: {', '.join(GEO_MAP.keys())}", flush=True)

    # Register the persistent alert View so button interactions survive restarts.
    try:
        bot.add_view(monitor.AlertView())
    except Exception as e:
        print(f"[monitor] Failed to register AlertView: {e}", flush=True)

    # Start the background monitor loop (once).
    if _monitor_task is None:
        _monitor_task = monitor.create_monitor_task(bot)
        _monitor_task.start()
        enabled = [c for c, g in monitor.GEOS.items() if g["monitor"]]
        print(
            f"[monitor] Loop started (interval={monitor.INTERVAL_MINUTES}min). "
            f"Enabled GEOs: {enabled or 'none'}",
            flush=True,
        )


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        raise SystemExit(1)
    bot.run(DISCORD_BOT_TOKEN)
