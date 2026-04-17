# Discord GEO Bot + Monitor

Discord bot that tracks which mirror `chancer.bet` redirects to for 7 countries,
and continuously monitors those mirrors for ISP-level blocks. Detection uses
NordVPN tunnels (redirect discovery), Decodo residential proxies (HTTP health),
and RIPE Atlas DNS measurements (DNS hijack detection).

## Architecture

```
Discord           discord_bot.py (EC2)           monitor.py (same process)
─────             ────────────────────           ────────────────────────
/check-redirect   ─ HTTP 127.0.0.1:8080 ──>    redirect_checker.py
                    NordVPN tunnel per GEO        ↓ mirror result
                  <── mirror saved to             ↓
                      redirects.json ────────>  reads redirects.json
                                                  ↓
                                                every 10 min:
                                                  HU: Decodo per-ASN consensus
                                                  GR/PL: RIPE Atlas (04:00/16:00 UTC)
                                                  DK/NO/FR/AE: Decodo + RIPE confirm
                                                  ↓
                                                alert via Discord webhook
                                                  [Ignore] [Mirror updated]
```

Both `discord_bot.py` and `redirect_checker.py` run on a single t2.micro in
`eu-central-1`. The monitor runs as a `discord.ext.tasks` loop inside the bot
process.

## Slash commands

| Command | What it does |
|---|---|
| `/check-redirect geo:HU` | Pauses the monitor, runs the VPN-based redirect check, saves result to `redirects.json`, updates the channel topic with current mirrors, resumes monitor after 60s |
| `/redirect-status` | Shows the current saved redirects for all GEOs |
| `/mirror-test url:domain.com geo:HU` | On-demand block test for any domain. Runs Decodo HTTP + RIPE DNS checks and returns a traffic light verdict. Simulation only — does not affect live monitoring |

## Monitor check methods

The background monitor uses three different strategies depending on the country:

### Hungary (`hu_consensus`)
- **Decodo only** — Hungary uses HTTP-level blocking (SZTFH block pages), not DNS hijacking, so RIPE Atlas is not used
- Decodo residential proxy with **per-ASN routing** every 10 minutes
- Checks 4 Hungarian ISP ASNs in parallel: Magyar Telekom, Vodafone, DIGI, Yettel
- **ALL 4** must report blocked in the same cycle for it to count as a failure
- **6 consecutive** all-blocked cycles required before an alert fires
- Block detection: SZTFH government block pages, SSL resets, CF hard blocks, ISP redirects

### Greece & Poland (`ripe_reliable_asn`)
- RIPE Atlas DNS measurements **twice daily** at 04:00 and 16:00 UTC
- Reliable ASN (Cosmote for GR, Orange Polska for PL) plus peer ASNs
- If reliable ASN is hijacked (resolved IP != 104.24.14.93), enters pending-confirmation
- **3 confirmed results**, each spaced **60 minutes apart**, required before alert
- **Rapid escalation**: if a NEW ASN becomes hijacked mid-window, alert fires immediately

### DK, NO, FR, AE (`decodo_plus_ripe_confirm`)
- Primary: Decodo HTTP check every 10 minutes (single proxy, no per-ASN)
- After **3 consecutive** Decodo failures, escalates to 4-ASN RIPE confirmation
- ALL 4 configured confirm ASNs must show 100% DNS hijack before alert fires
- Weekly proactive RIPE sweep on **Monday 04:00 UTC**

## /mirror-test — on-demand block testing

`/mirror-test url:wolfycasino.com geo:FR` runs an independent check against any domain
in any configured country. It does not affect live monitoring, redirects, or state.

Both Decodo HTTP and RIPE DNS checks run concurrently. The result is a traffic light embed:

| Verdict | Condition |
|---|---|
| **GREEN** | Decodo HTTP up AND RIPE shows 0 ASNs hijacked |
| **ORANGE** | Any hijacked ASNs but < 50%, OR Decodo red/orange with clean DNS |
| **RED** | RIPE >= 50% ASNs hijacked (regardless of Decodo) |

For **Hungary**, only Decodo runs (no RIPE) because HU uses HTTP-level blocking, not DNS hijacking.
If RIPE is unavailable (no API key, no credits), falls back to Decodo-only verdict.

## Alert system

- Alerts fire via Discord webhook to a dedicated alerts channel
- Each alert has two persistent buttons:
  - **Ignore** — mute alerts for this GEO for 1 hour
  - **Mirror updated** — reset monitoring to the new mirror in `redirects.json`
- No "up" or "recovery" notifications — state clears silently
- Re-alert after 4 hours if outage is still active and unacknowledged
- Buttons survive bot restarts (persistent `custom_id` views)

## Repo layout

| File | Purpose |
|---|---|
| `discord_bot.py` | Bot with slash commands, pause-on-check, monitor wiring |
| `monitor.py` | Background health monitor (Decodo, RIPE Atlas, state machine, alerts) |
| `config.yaml` | GEO definitions, ASN lists, monitor tuning parameters |
| `redirects.json` | Runtime state — current mirror per GEO (not in git) |
| `monitor_state.json` | Runtime state — failure counts, pending windows, alert flags (not in git) |
| `ec2_setup.sh` | One-shot EC2 provisioning (OpenVPN + Flask checker + systemd) |
| `deploy.sh` | `git push` → SSH to EC2 → `git pull` → restart service |
| `testmode.py` | Switch to local dev (opens SG port, stops EC2 bot) |
| `livemode.py` | Switch back to production (closes SG port, starts EC2 bot) |
| `.env.example` | Template for secrets — copy to `.env` and fill in |

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Bot | Discord gateway connection |
| `DISCORD_ALERT_WEBHOOK_URL` | Monitor | Webhook URL for the alerts channel |
| `REDIRECT_CHECKER_URL` | Bot | EC2 redirect checker (`http://127.0.0.1:8080` on EC2) |
| `REDIRECT_CHECKER_KEY` | Bot | Shared secret for the checker API |
| `PROXY_HOST` / `PROXY_PORT` | Monitor | Decodo residential proxy endpoint |
| `PROXY_USERNAME` / `PROXY_PASSWORD` | Monitor | Decodo credentials |
| `RIPE_ATLAS_API_KEY` | Monitor | RIPE Atlas measurement API key |

## Development workflow

Only one bot instance can run per token. Stop the EC2 bot before running locally.

```bash
# Switch to local dev
python testmode.py       # stops EC2 bot, opens port 8080 for your IP
python discord_bot.py    # run locally

# When done
python livemode.py       # starts EC2 bot, closes port 8080
```

Deploy to production:

```bash
git add config.yaml discord_bot.py monitor.py
git commit -m "describe change"
bash deploy.sh           # git push → EC2 git pull → restart service
```

Or manually on EC2:

```bash
cd /opt/discord-bot && sudo git pull && sudo systemctl restart discord-bot
```

## EC2 management

```bash
# SSH in
ssh -i ~/.ssh/geo-redirect-checker.pem ec2-user@63.178.175.200

# Service commands
sudo systemctl start discord-bot
sudo systemctl stop discord-bot
sudo systemctl restart discord-bot
sudo systemctl status discord-bot

# Logs
sudo journalctl -u discord-bot -f          # live tail
sudo journalctl -u discord-bot -n 50       # last 50 lines
tail -f /opt/discord-bot/logs/monitor.log  # monitor log file
```

## Infrastructure

| Resource | Value |
|---|---|
| AWS account | 548010038081 |
| Region | eu-central-1 (Frankfurt) |
| Instance | `i-0f5465cf4a2cb1556` (t2.micro) |
| Elastic IP | `63.178.175.200` |
| Security group | `sg-0c198e9f76e0ffde1` |
| SSH key | `~/.ssh/geo-redirect-checker.pem` |

Systemd services on EC2:

| Service | What it runs |
|---|---|
| `redirect-checker.service` | `/opt/redirect_checker.py` — Flask API on port 8080 (NordVPN tunnel per request) |
| `discord-bot.service` | `/opt/discord-bot/discord_bot.py` — Discord bot + background monitor |

## Adding a new GEO

1. Add a `{code, name, flag, check_method, monitor, asns}` entry to `config.yaml`.
2. Add a NordVPN server mapping to `SERVER_MAP` in `ec2_setup.sh` / `redirect_checker.py`.
3. Run `/check-redirect` for the new GEO to populate `redirects.json` (the monitor can't check a GEO without a known mirror).
4. `bash deploy.sh` to push and restart.
