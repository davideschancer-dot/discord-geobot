# Discord GEO Bot

Discord slash-command bot that reports which mirror `chancer.bet` currently
redirects to for each configured country. Detection runs through a NordVPN
OpenVPN tunnel on an EC2 instance, so it sees exactly what a real user in
that country would see.

## Architecture

```
Discord          discord_bot.py (EC2)          redirect_checker.py (EC2)
─────            ────────────────────          ─────────────────────────
 /check-redirect  ── HTTP via 127.0.0.1 ──>  GET /check?geo=hu  ──┐
                                                                   │
                                              spin up NordVPN HU   │
                                              curl chancer.bet     │
                                              parse Location       │
                                              tear down tunnel     │
                                             <──── {mirror} ───────┘
 ← "HU → chancer8.xyz"
```

Both services run on a single t2.micro in `eu-central-1`. Port 8080 (the
checker) is locked to localhost — only the bot on the same box can reach it.

## Slash commands

| Command | What it does |
|---|---|
| `/check-redirect geo:HU` | Asks whether to purge channel history, then runs the VPN-based redirect check and saves the result to `redirects.json` |
| `/redirects` | Prints the current saved redirects |
| `/redirect-table` | Prints the current redirects as a fixed-width table |
| `/set-redirect geo:HU mirror:chancer8.xyz` | Manually override a redirect (marked `manual`) |

## Repo layout

| File | Purpose |
|---|---|
| `discord_bot.py` | The bot. Reads `config.yaml` and `.env`. |
| `config.yaml` | GEOs the bot knows about (code, name, flag). |
| `ec2_setup.sh` | One-shot provisioning script for the EC2 checker (OpenVPN + Flask API + systemd). |
| `deploy.sh` | `git push` → SSH to EC2 → `git pull` → restart bot. |
| `testmode.py` | Switch to local dev (opens SG, stops EC2 bot). |
| `livemode.py` | Switch back to production (closes SG, starts EC2 bot). |
| `.env.example` | Template — copy to `.env` and fill in. |

## Development workflow

Because Discord only allows one connection per bot token, you must stop
the EC2 bot before running locally — otherwise the two instances fight
and you'll see session-invalidation loops.

The `testmode.py` / `livemode.py` scripts handle the switch:

```bash
# Switch to local dev
python testmode.py

# Make changes, run locally
python discord_bot.py

# Ctrl-C to stop, then switch back
python livemode.py
```

Or just push straight to prod without local testing:

```bash
git add -A && git commit -m "describe change"
bash deploy.sh          # push → pull on EC2 → restart service
```

## Adding a new GEO

1. Add a `{code, name, flag}` entry to `config.yaml` under `geos`.
2. Add a NordVPN server mapping to `SERVER_MAP` in `ec2_setup.sh`.
   Not all countries have servers numbered `69` — check with:
   ```bash
   for n in 69 100 150 200 300 500; do
     echo -n "$n: "
     curl -s -o /dev/null -w "%{http_code}\n" \
       "https://downloads.nordcdn.com/configs/files/ovpn_udp/servers/<cc>${n}.nordvpn.com.udp.ovpn"
   done
   ```
   Use the first server number that returns `200`.
3. SSH into the EC2 and update `SERVER_MAP` in `/opt/redirect_checker.py`
   (or re-run `ec2_setup.sh` after committing the change).
4. `bash deploy.sh` to push the `config.yaml` update.

## Infrastructure

| Resource | Value |
|---|---|
| AWS account | 548010038081 |
| Region | eu-central-1 (Frankfurt) |
| Instance | `i-0f5465cf4a2cb1556` (t2.micro) |
| Elastic IP | `63.178.175.200` |
| Security group | `sg-0c198e9f76e0ffde1` (SSH open to your IP; port 8080 closed) |
| SSH key | `~/.ssh/geo-redirect-checker.pem` |

Systemd services on EC2:

| Service | What it runs |
|---|---|
| `redirect-checker.service` | `/opt/redirect_checker.py` — Flask API on port 8080 |
| `discord-bot.service` | `/opt/discord-bot/discord_bot.py` — Discord gateway connection |

Secrets on EC2 (not in git):

| File | Contains |
|---|---|
| `/etc/openvpn/client/credentials.txt` | NordVPN service username + password (root-only, 0600) |
| `/opt/discord-bot/.env` | Discord bot token + checker URL/key (root-only, 0600) |

## Initial setup (if rebuilding from scratch)

1. Launch a t2.micro in `eu-central-1`, SSH in.
2. Export the env vars and run the provisioning script:
   ```bash
   export NORDVPN_SERVICE_USER=...
   export NORDVPN_SERVICE_PASS=...
   export API_KEY=...
   sudo -E bash ec2_setup.sh
   ```
3. Clone the bot on the EC2 and create its systemd unit:
   ```bash
   sudo git clone https://github.com/davideschancer-dot/discord-geobot.git /opt/discord-bot
   sudo pip3 install -r /opt/discord-bot/requirements.txt
   # Write /opt/discord-bot/.env with DISCORD_BOT_TOKEN + REDIRECT_CHECKER_URL/KEY
   # Write /etc/systemd/system/discord-bot.service
   sudo systemctl enable --now discord-bot
   ```
4. Invite the bot to your Discord server with scopes
   `bot applications.commands` and permissions `Send Messages`,
   `Embed Links`, `Manage Messages`, `Read Message History`.
