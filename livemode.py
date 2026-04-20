"""
livemode.py — switch back to production (EC2)
-----------------------------------------------
Stops any local bot process, closes port 8080 on the EC2 security
group (locks the redirect checker to localhost-only), revokes the
current IP from port 22 (preserving the static admin SSH rule), and
starts the EC2 discord-bot service.

Run: python livemode.py
"""
import json
import subprocess
import sys
from urllib.request import urlopen

REGION = "eu-central-1"
SECURITY_GROUP_ID = "sg-0c198e9f76e0ffde1"
EC2_HOST = "ec2-user@63.178.175.200"
SSH_KEY = "~/.ssh/geo-redirect-checker.pem"
CHECKER_PORT = 8080
SSH_PORT = 22


def run(cmd, check=True):
    """Run a shell command and return its stdout, or None if it failed."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


def _detect_ip():
    try:
        return urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip()
    except Exception as e:
        print(f"  Failed to detect public IP: {e}")
        return None


def main():
    # 1. Kill any local bot processes so we don't end up with two bots
    #    connected to the same Discord token.
    print("[1/4] Stopping any local bot processes...")
    # On Windows, taskkill kills all python.exe; on Unix we use pkill.
    if sys.platform == "win32":
        subprocess.run("taskkill /F /IM python.exe", shell=True, capture_output=True)
    else:
        subprocess.run("pkill -f discord_bot.py", shell=True, capture_output=True)
    print("  Local bots stopped.")

    # 2. Start the EC2 discord-bot service. Must happen BEFORE we revoke our
    #    own SSH rule below — otherwise we lock ourselves out mid-script.
    print("[2/4] Starting EC2 discord-bot service...")
    run(f'ssh -o StrictHostKeyChecking=no -i {SSH_KEY} {EC2_HOST} "sudo systemctl start discord-bot"')

    # 3. Confirm it's running before locking down access.
    print("[3/4] Verifying bot is running...")
    status = run(
        f'ssh -o StrictHostKeyChecking=no -i {SSH_KEY} {EC2_HOST} '
        f'"sudo systemctl is-active discord-bot"'
    )
    print(f"  discord-bot status: {status}")
    if status != "active":
        print("  WARNING: service did not become active — leaving SG open so you can SSH and debug.")
        return

    # 4. Lock the security group back down (only after the service is up).
    #    - Port 8080: remove ALL rules (the EC2 bot uses 127.0.0.1 internally).
    #    - Port 22: remove only the current IP rule that testmode.py added —
    #      any other SSH rule (e.g. a static admin IP) is preserved.
    print("[4/4] Closing dev access on EC2 security group...")
    my_ip = _detect_ip()
    sg_json = run(
        f"aws ec2 describe-security-groups --region {REGION} "
        f"--group-ids {SECURITY_GROUP_ID} --output json"
    )
    if sg_json:
        data = json.loads(sg_json)
        for rule in data["SecurityGroups"][0].get("IpPermissions", []):
            if rule.get("IpProtocol") != "tcp":
                continue
            port = rule.get("FromPort")
            if port == CHECKER_PORT:
                # Revoke every CIDR — 8080 must be locked down completely.
                for ip_range in rule.get("IpRanges", []):
                    cidr = ip_range["CidrIp"]
                    run(
                        f"aws ec2 revoke-security-group-ingress --region {REGION} "
                        f"--group-id {SECURITY_GROUP_ID} --protocol tcp "
                        f"--port {CHECKER_PORT} --cidr {cidr}",
                        check=False,
                    )
                    print(f"  Removed port {CHECKER_PORT} rule for {cidr}")
            elif port == SSH_PORT and my_ip:
                # Only revoke the current IP — preserve any other SSH rules.
                target = f"{my_ip}/32"
                for ip_range in rule.get("IpRanges", []):
                    if ip_range["CidrIp"] == target:
                        run(
                            f"aws ec2 revoke-security-group-ingress --region {REGION} "
                            f"--group-id {SECURITY_GROUP_ID} --protocol tcp "
                            f"--port {SSH_PORT} --cidr {target}",
                            check=False,
                        )
                        print(f"  Removed port {SSH_PORT} rule for {target}")
    print(f"  Port {CHECKER_PORT} is now locked to localhost only.")

    print()
    print("=" * 60)
    print("LIVE MODE ACTIVE")
    print("=" * 60)
    print("The bot is running on EC2 and accessible in Discord.")
    print()
    print("To switch back to local dev:")
    print("    python testmode.py")


if __name__ == "__main__":
    main()
