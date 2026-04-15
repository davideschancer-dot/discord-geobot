"""
livemode.py — switch back to production (EC2)
-----------------------------------------------
Stops any local bot process, closes port 8080 on the EC2 security
group (locks the redirect checker to localhost-only), and starts
the EC2 discord-bot service.

Run: python livemode.py
"""
import subprocess
import sys

REGION = "eu-central-1"
SECURITY_GROUP_ID = "sg-0c198e9f76e0ffde1"
EC2_HOST = "ec2-user@63.178.175.200"
SSH_KEY = "~/.ssh/geo-redirect-checker.pem"
PORT = 8080


def run(cmd, check=True):
    """Run a shell command and return its stdout, or None if it failed."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return None
    return result.stdout.strip()


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

    # 2. Remove ALL IP rules on port 8080 (fully lock it from the internet).
    #    The EC2 bot accesses the checker via 127.0.0.1 so it doesn't need
    #    the port open externally.
    print(f"[2/4] Closing port {PORT} on EC2 security group...")
    import json
    result = run(
        f"aws ec2 describe-security-groups --region {REGION} "
        f"--group-ids {SECURITY_GROUP_ID} --output json"
    )
    if result:
        data = json.loads(result)
        for rule in data["SecurityGroups"][0].get("IpPermissions", []):
            if rule.get("FromPort") == PORT and rule.get("IpProtocol") == "tcp":
                for ip_range in rule.get("IpRanges", []):
                    cidr = ip_range["CidrIp"]
                    run(
                        f"aws ec2 revoke-security-group-ingress --region {REGION} "
                        f"--group-id {SECURITY_GROUP_ID} --protocol tcp "
                        f"--port {PORT} --cidr {cidr}",
                        check=False,
                    )
                    print(f"  Removed rule for {cidr}")
    print(f"  Port {PORT} is now locked to localhost only.")

    # 3. Start the EC2 discord-bot service.
    print("[3/4] Starting EC2 discord-bot service...")
    run(f'ssh -o StrictHostKeyChecking=no -i {SSH_KEY} {EC2_HOST} "sudo systemctl start discord-bot"')

    # 4. Confirm it's running.
    print("[4/4] Verifying bot is running...")
    status = run(
        f'ssh -o StrictHostKeyChecking=no -i {SSH_KEY} {EC2_HOST} '
        f'"sudo systemctl is-active discord-bot"'
    )
    print(f"  discord-bot status: {status}")

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
