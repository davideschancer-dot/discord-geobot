# GEO Monitor — Reference Specification

## What this system does

Chancer operates a gambling website behind mirror domains (chancer1.xyz, chancer5.xyz, etc.) with a router domain (chancer.bet) that redirects users per country. ISPs and regulators in seven countries block access to these mirrors via DNS hijacking, HTTP interception, or government-mandated block pages. The monitor detects when a mirror becomes inaccessible in a specific country and alerts the team via Discord so they can rotate to a working mirror.

## The seven countries and how each is checked

### Hungary (HU) — Decodo all-ASN consensus
Hungary uses SZTFH government gambling block pages served at the ISP level. Detection uses Decodo residential proxy requests routed through four specific Hungarian ISPs (Magyar Telekom AS5483, Vodafone AS21334, DIGI AS20845, Yettel AS213155). All four ASNs are checked in parallel on every cycle (every 10 minutes). ALL FOUR must independently return a block signal (SZTFH page markers, SSL reset, redirect to unknown domain) for the mirror to be considered down. If even one ASN returns the site as accessible, the mirror is not blocked nationwide and no alert fires. Six consecutive all-four-blocked cycles are required before an alert fires. This is the strictest threshold because Hungary is the most actively monitored country.

### Greece (GR) — RIPE Atlas reliable-ASN
Greece uses ISP DNS hijacking. Detection uses RIPE Atlas DNS measurements targeted at probes inside OTE/Cosmote (AS1241), the most reliable Greek ISP for detecting blocks. A measurement asks probes on that ASN to resolve the mirror domain and report the IP they get. If the resolved IP is anything other than the real Cloudflare IP (104.24.14.93), the DNS is hijacked. Checks run twice daily (04:00 and 16:00 UTC). A single hijacked observation opens a pending confirmation window — three confirmed hijacked results, each spaced 60 minutes apart, are required before an alert fires. Additional peer ASNs (Forthnet AS6799, Vodafone GR AS3329, Wind Hellas AS25472) feed a "new ASN hijacked" escalation signal but are not required for the primary alert.

### Poland (PL) — RIPE Atlas reliable-ASN
Same method as Greece. Reliable ASN is Orange Polska (AS5617). Peer ASNs: GTS/T-Mobile AS5588, T-Mobile PL AS12912, Vectra AS29314, Multimedia Polska AS21021. Same twice-daily schedule, same 3-attempt pending window.

### Denmark (DK) — Decodo HTTP + weekly RIPE + 4-ASN confirm
Denmark has court-ordered DNS blocks (Lotteritilsynet). Primary detection is Decodo residential proxy HTTP requests every 10 minutes (same as the legacy HTTP check — tests whether the mirror loads through a Danish residential IP). When two consecutive Decodo failures occur, the system escalates to a 4-ASN RIPE Atlas confirmation: DNS measurements are run against four Danish ISPs (TDC AS3292, Telenor DK AS3308, Stofa/Norlys AS9158, Hiper AS31027). ALL FOUR ASNs must show 100% DNS hijack (every probe on every ASN resolves to the wrong IP) for an alert to fire. A weekly RIPE sweep also runs on Monday 04:00 UTC as a proactive check regardless of Decodo status.

### Norway (NO) — Decodo HTTP + weekly RIPE + 4-ASN confirm
Same method as Denmark. Court-ordered DNS blocks (Lotteritilsynet). Confirm ASNs: Uninett/Sikt AS2116, Telenor Norway AS5381, Telia Norway AS12929, Get/Telia AS29695.

### France (FR) — Decodo HTTP + weekly RIPE + 4-ASN confirm
Same method as Denmark. ANJ DNS blocks. Confirm ASNs: Orange FR AS3215, Bouygues Telecom AS5410, Free/ProXad AS12322, SFR AS15557.

### UAE (AE) — Decodo HTTP + weekly RIPE + 4-ASN confirm
Same method as Denmark. National DNS hijack — block pages served at Cloudflare IPs 104.16.130.238 / 104.16.131.238 (these are NOT the real site; any resolved IP other than 104.24.14.93 counts as hijacked). Confirm ASNs: du/EITC AS5384, FLAG Telecom/Etisalat AS15412, Etisalat AS15802, Alibaba UAE AS45102.

## Core design principles

### No false positives — confirmed alerts only
Every alert path requires multi-source confirmation before firing. A single proxy error, a single DNS timeout, a single ASN returning a bad result — none of these produce an alert on their own. The system is designed to stay silent unless a block is genuinely confirmed across multiple independent vantage points.

### No "up" or "recovery" notifications
The system only alerts on confirmed outages. When a mirror comes back up, it silently clears internal state. The team does not receive "recovered" messages — they create noise and the team already knows when they've rotated a mirror.

### Alerts post to a dedicated alerts channel
Outage alerts post to a dedicated alerts channel (not the dashboard channel). The bot prefers posting directly to a channel ID (`DISCORD_ALERT_CHANNEL_ID`) so it can attach the persistent Ignore / Mirror updated buttons — Discord rejects interactive components on messages from non-application webhooks. A webhook URL (`DISCORD_ALERT_WEBHOOK_URL`) is supported as a fallback for environments without channel access, but in that mode buttons are not attached. Each alert has two persistent buttons (when channel posting is used): "Ignore" (mute alerts for this GEO for 1 hour) and "Mirror updated" (reset monitoring to the new mirror in redirects.json). Re-alerts fire after 4 hours if the outage is still active and unacknowledged.

### The dashboard is separate from monitoring
The dashboard (posted to a dedicated channel) shows the current state of all GEOs and their active mirrors. It is refreshed only by explicit command (/check-redirect or /redirect-status), never by the monitor. The monitor writes to monitor_state.json; the dashboard reads from redirects.json and state.

## Checker vs Monitor — sequencing and relationship

The checker and the monitor are two distinct functions running on the same EC2 instance. They are not peers — the checker is a prerequisite for the monitor.

The checker (/check-redirect) is an on-demand, mostly one-off operation. It resolves which mirror domain chancer.bet currently redirects to for a given GEO by routing traffic through a NordVPN tunnel on the EC2 host. The result is written to redirects.json. The checker does not run on a schedule — it is triggered manually via the /check-redirect Discord command when the team needs to discover or rotate a mirror.

The monitor runs continuously in the background. It reads redirects.json to learn which mirror to watch for each GEO, then checks accessibility using the per-country detection methods described above. The monitor depends on the checker's output — it cannot monitor a GEO that has no mirror entry in redirects.json.

Startup sequence: the checker must run first for each GEO to populate redirects.json before the monitor has anything to watch. Once all seven GEOs have a mirror entry, the monitor can run. On a fresh deploy, run /check-redirect for each GEO before enabling monitor: true.

Pause-on-check: when /check-redirect is invoked, the bot must stop the monitor loop before running the check. After the checker writes its result to redirects.json, the monitor restarts after a 60-second delay. This prevents the monitor from ticking mid-update and evaluating a mirror that is about to change. The sequence is: stop monitor → run checker → write redirects.json → wait 60s → restart monitor.

## Architecture

Two files contain all detection logic:
- monitor.py — check methods, RIPE Atlas client, block-page taxonomy, state machine, alert dispatch, cycle loop with per-country cadence gating. Runs as a discord.ext.tasks loop inside the bot process (not a separate service).
- config.yaml — maps each GEO to its check method, ASN list, and monitor on/off flag. Tuning parameters (thresholds, schedules, timing) live here too.

The bot (discord_bot.py) hosts two slash commands:
- /check-redirect — stops the monitor loop, calls the EC2 NordVPN redirect checker to resolve the current mirror for a GEO, saves to redirects.json, purges the dashboard channel, posts a fresh dashboard. The monitor restarts automatically after 60 seconds.
- /redirect-status — purges and reposts the dashboard without checking. Does not affect the monitor loop.
- /mirror-test url:<url> geo:<XX> — runs a real Decodo HTTP + RIPE DNS check against the given URL/country, returns a traffic-light verdict embed. When the verdict is RED, additionally dispatches a `[TEST]`-labelled alert through the same `_send_alert` + MonitorState write path the live monitor uses (same channel, same Ignore / Mirror updated buttons). State writes go under a sentinel `SIM` geo so per-country state is never modified. The log record carries `sim=true`. There is no parallel mock pipeline — only the storage key differs.

The redirect checker is a local HTTP service on the same EC2 host (port 8080, localhost-only, behind NordVPN tunnel). It resolves where chancer.bet lands for a given country code. It is called by /check-redirect, not by the monitor.

## Environment

Runs on AWS EC2 (instance i-0f5465cf4a2cb1556, 63.178.175.200). Bot process managed by systemd (discord-bot.service). Working directory: /opt/geo-monitor. Deploy via deploy.sh: git pull to /opt/discord-bot (GitHub clone), copy .py + .yaml to /opt/geo-monitor, restart service.

Required credentials (.env, not in git):
- DISCORD_BOT_TOKEN — bot login
- DISCORD_ALERT_CHANNEL_ID — channel where the bot posts outage alerts (preferred; required for buttons)
- DISCORD_ALERT_WEBHOOK_URL — webhook fallback when channel ID is unset (no buttons)
- PROXY_USERNAME / PROXY_PASSWORD — Decodo residential proxy (gate.decodo.com:10001)
- RIPE_API_KEY — RIPE Atlas measurement creation (free tier sufficient)
- REDIRECT_CHECKER_URL / REDIRECT_CHECKER_KEY — EC2 NordVPN redirect checker

State files (in /opt/geo-monitor, not in git):
- redirects.json — current active mirror per GEO
- monitor_state.json — per-GEO monitoring state (status, failures, pending windows, history)
