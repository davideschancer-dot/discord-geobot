"""
testmode.py — switch to local development
------------------------------------------
Stops the EC2 discord-bot service and opens port 8080 on the EC2
security group for YOUR current public IP, so the local bot can
reach the redirect checker.

Run: python testmode.py
"""
import subprocess
import sys
from urllib.request import urlopen

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
    # 1. Get current public IP
    print("[1/3] Detecting your public IP...")
    try:
        my_ip = urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip()
        print(f"  Your IP: {my_ip}")
    except Exception as e:
        print(f"  Failed: {e}")
        sys.exit(1)

    # 2. Open port 8080 on the security group for your IP
    print(f"[2/3] Opening port {PORT} on EC2 security group for {my_ip}/32...")
    run(
        f"aws ec2 authorize-security-group-ingress --region {REGION} "
        f"--group-id {SECURITY_GROUP_ID} --protocol tcp --port {PORT} "
        f"--cidr {my_ip}/32",
        check=False,  # might already exist — that's fine
    )
    print(f"  Port {PORT} is now reachable from {my_ip}")

    # 3. Stop the EC2 discord-bot service so it doesn't conflict with the local bot
    print("[3/3] Stopping EC2 discord-bot service...")
    run(f'ssh -o StrictHostKeyChecking=no -i {SSH_KEY} {EC2_HOST} "sudo systemctl stop discord-bot"')
    print("  EC2 bot stopped.")

    print()
    print("=" * 60)
    print("TEST MODE ACTIVE")
    print("=" * 60)
    print("You can now run the bot locally:")
    print("    python discord_bot.py")
    print()
    print("When done, switch back with:")
    print("    python livemode.py")


if __name__ == "__main__":
    main()
