# Discord GEO Bot

Discord slash-command bot that reports which mirror `chancer.bet` currently
redirects to for each configured country. Detection runs through a NordVPN
OpenVPN tunnel on an EC2 instance, so it sees exactly what a real user in
that country would see.

## Architecture

```
Discord          discord_bot.py (EC2)             redirect_checker.py (EC2)
─────            ──────────────────                ─────────────────────────
 /check-redirect  ──── HTTP ────>  GET /check?geo=hu  ──┐
                                                        │
                                   spin up NordVPN HU   │
                                   curl chancer.bet     │
                                   parse Location       │
                                   tear down tunnel     │
                                  <──── {mirror} ───────┘
 ← "HU → chancer8.xyz"
```

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
| `ec2_setup.sh` | Provisions the EC2 checker: installs OpenVPN, deploys `/opt/redirect_checker.py`, creates the systemd service. |
| `deploy.sh` | `git push` → SSH to EC2 → `git pull` → restart. |
| `.env.example` | Template — copy to `.env` and fill in. |

## Adding a new GEO

1. Add a `{code, name, flag}` entry to `config.yaml` under `geos`.
2. Add a server mapping to `SERVER_MAP` in `ec2_setup.sh` (find a working NordVPN server number with `curl -I https://downloads.nordcdn.com/configs/files/ovpn_udp/servers/<cc>69.nordvpn.com.udp.ovpn` — if 404, try 100/150/200/etc).
3. Re-deploy the EC2 checker (or manually edit `/opt/redirect_checker.py` on the instance).
4. Deploy the bot: `bash deploy.sh`

## Development

Local dev loop:
```
# Stop the EC2 bot so Discord doesn't get two connections on the same token
ssh -i ~/.ssh/geo-redirect-checker.pem ec2-user@63.178.175.200 "sudo systemctl stop discord-bot"

# Run locally
python discord_bot.py

# When done testing, restart the EC2 bot
ssh -i ~/.ssh/geo-redirect-checker.pem ec2-user@63.178.175.200 "sudo systemctl start discord-bot"
```

Or deploy to EC2:
```
bash deploy.sh
```

## Infrastructure

- **EC2 instance**: `i-0f5465cf4a2cb1556` in `eu-central-1` (Frankfurt), t2.micro
- **Elastic IP**: `63.178.175.200`
- **Security group**: `sg-0c198e9f76e0ffde1`
- **SSH key**: `~/.ssh/geo-redirect-checker.pem`
- **AWS account**: 548010038081

Services on EC2:
- `redirect-checker.service` → Flask API on port 8080 (the GEO check endpoint)
- `discord-bot.service` → runs `discord_bot.py` (connects to Discord)
