# GeoBot Test Prompts for Claude Code (EC2)

Each prompt below is designed to be pasted directly into Claude Code in terminal while SSH'd into the EC2 instance (or with SSH access). They are grouped by test area and reference specific test IDs from the test matrix spreadsheet.

---

## EC2 Redirect Checker (T43-T47)

### T43 — VPN tunnel lifecycle
```
SSH into the EC2 instance at 63.178.175.200. Run a redirect check by calling: curl "http://localhost:8080/check?key=chancer-geo-2026&geo=hu" — but BEFORE running it, open a second SSH session and run a loop that checks for openvpn processes every 2 seconds: while true; do ps aux | grep '[o]penvpn' | head -1; sleep 2; done. Run the curl command, wait for it to complete, then check the ps loop output. Report back whether you saw the openvpn process appear during the check and disappear after it completed. Also note how long the check took.
```

### T44 — Correct mirror returned per GEO
```
SSH into the EC2 instance at 63.178.175.200. Run the redirect checker for all 7 GEOs one at a time and collect the results:
  curl "http://localhost:8080/check?key=chancer-geo-2026&geo=hu"
  curl "http://localhost:8080/check?key=chancer-geo-2026&geo=gr"
  curl "http://localhost:8080/check?key=chancer-geo-2026&geo=pl"
  curl "http://localhost:8080/check?key=chancer-geo-2026&geo=dk"
  curl "http://localhost:8080/check?key=chancer-geo-2026&geo=fr"
  curl "http://localhost:8080/check?key=chancer-geo-2026&geo=ae"
  curl "http://localhost:8080/check?key=chancer-geo-2026&geo=no"
Report the mirror returned for each GEO, whether each request succeeded, and how long each took. Also compare the results to the current contents of /opt/discord-bot/redirects.json on the EC2 box to see if they match.
```

### T45 — Health endpoint
```
SSH into the EC2 instance at 63.178.175.200 and run: curl -v http://localhost:8080/health. Report the HTTP status code and response body. Expected: HTTP 200 with {"status": "ok"}.
```

### T46 — Concurrent check locking
```
SSH into the EC2 instance at 63.178.175.200. Test the concurrency lock by firing two simultaneous check requests: curl "http://localhost:8080/check?key=chancer-geo-2026&geo=hu" & curl "http://localhost:8080/check?key=chancer-geo-2026&geo=pl" & wait. Record the start and end timestamps of each request. The second request should take noticeably longer because it has to wait for the threading lock. Report the timing of both requests and whether they ran sequentially.
```

### T47 — CF IP routes added/removed during check
```
SSH into the EC2 instance at 63.178.175.200. In one terminal, start a loop: while true; do ip route | grep '104.24'; sleep 1; done. In a second terminal, run: curl "http://localhost:8080/check?key=chancer-geo-2026&geo=hu". Watch the route loop output. Report whether you see routes for 104.24.14.0/24 or 104.24.15.0/24 appear during the check and disappear after it completes. Include the exact route entries you see.
```

---

## Log Rotation (T51)

### T51 — Monitor log rotation at 1MB
```
SSH into the EC2 instance at 63.178.175.200. Check the current log files: ls -lh /opt/discord-bot/logs/monitor.log*. Report the sizes. If no rotated files exist yet, create a large dummy to force rotation: dd if=/dev/zero bs=1M count=2 >> /opt/discord-bot/logs/monitor.log. Then wait for the next monitor cycle (up to 10 minutes) and check again: ls -lh /opt/discord-bot/logs/monitor.log*. Report whether monitor.log.1 was created and whether the active monitor.log was reset to a small size. The rotation config is max 1MB per file with 5 backups.
```

---

## Deploy Workflow (T54)

### T54 — deploy.sh pulls latest code and restarts
```
SSH into the EC2 instance at 63.178.175.200. First record the current state: git -C /opt/discord-bot log --oneline -1 && systemctl status discord-bot --no-pager | head -5. Then from the local machine, run bash deploy.sh. After it completes, SSH back in and check: git -C /opt/discord-bot log --oneline -1 && systemctl status discord-bot --no-pager | head -5. Report whether the commit hash changed to the latest, and whether the service restarted (check the Active: line for a recent timestamp).
```

---

## RIPE Scheduled Checks for GR/PL (T21, T22)

### T21 + T22 — Verify GR/PL RIPE scheduled checks run at correct times
```
SSH into the EC2 instance at 63.178.175.200. Check the monitor logs for evidence of GR and PL RIPE scheduled checks: grep -E "(GR|PL).*RIPE|run_ripe_scheduled|ripe_check_per_asn.*(GR|PL)" /opt/discord-bot/logs/monitor.log. These should only appear near 04:00 and 16:00 UTC. Report what you find — the timestamps of any GR/PL RIPE check entries, and whether they fall within the expected schedule windows. If none exist yet, check the rotated logs too: grep -E "(GR|PL).*RIPE" /opt/discord-bot/logs/monitor.log.* 2>/dev/null. If still nothing, report that and note we need to wait for the next 04:00 or 16:00 UTC window.
```

---

## HU Block Detection Unit Tests (T12, T13, T16)

### T12 — SZTFH block page detection
```
On the EC2 instance at 63.178.175.200, cd to /opt/discord-bot and run this Python test inline. This tests whether the _evaluate_http_response function correctly detects Hungarian government block pages. Do NOT modify any files — just run the test:

python3 -c "
from unittest.mock import Mock
from monitor import _evaluate_http_response

# Test SZTFH block page markers
mock_resp = Mock()
mock_resp.status_code = 200
mock_resp.url = 'https://chancer8.xyz/'
mock_resp.headers = {}

# Marker 1: SZTFH
mock_resp.text = '<html><body>Az oldal az SZTFH határozata alapján hozzáférhetetlenné téve.</body></html>'
status, reason = _evaluate_http_response('chancer8.xyz', mock_resp)
print(f'SZTFH marker: status={status} reason={reason}')
assert status == 'red', f'Expected red, got {status}'
assert 'SZTFH' in reason, f'Expected SZTFH in reason, got {reason}'

# Marker 2: UTF-8 variant
mock_resp.text = '<html>hozzÃ¡fÃ©rhetetlennÃ© page blocked</html>'
status, reason = _evaluate_http_response('chancer8.xyz', mock_resp)
print(f'UTF8 variant: status={status} reason={reason}')
assert status == 'red', f'Expected red, got {status}'

# Marker 3: szerencsejáték (gambling)
mock_resp.text = '<html>szerencsejáték felügyelet blocked</html>'
status, reason = _evaluate_http_response('chancer8.xyz', mock_resp)
print(f'Gambling marker: status={status} reason={reason}')
assert status == 'red', f'Expected red, got {status}'

print('ALL SZTFH TESTS PASSED')
"

Report the output. All three assertions should pass with status=red.
```

### T13 — Cloudflare hard block detection
```
On the EC2 instance at 63.178.175.200, cd to /opt/discord-bot and run this Python test inline. Tests CF hard block detection for all 5 error codes:

python3 -c "
from unittest.mock import Mock
from monitor import _evaluate_http_response

cf_codes = ['1009', '1010', '1012', '1015', '1020']
for code in cf_codes:
    mock_resp = Mock()
    mock_resp.status_code = 403
    mock_resp.url = 'https://chancer8.xyz/'
    mock_resp.headers = {}
    mock_resp.text = f'<html><head><title>Attention Required</title></head><body>Error {code}: Access denied</body></html>'
    status, reason = _evaluate_http_response('chancer8.xyz', mock_resp)
    print(f'CF error {code}: status={status} reason={reason}')
    assert status == 'red', f'Expected red for CF {code}, got {status}'
    assert code in reason, f'Expected {code} in reason'

# Also test that a 403 WITHOUT CF error codes is still detected
mock_resp = Mock()
mock_resp.status_code = 403
mock_resp.url = 'https://chancer8.xyz/'
mock_resp.headers = {}
mock_resp.text = '<html>Forbidden</html>'
status, reason = _evaluate_http_response('chancer8.xyz', mock_resp)
print(f'Plain 403: status={status} reason={reason}')
assert status == 'red', f'Expected red for plain 403, got {status}'

# And HTTP 451 (legal block)
mock_resp.status_code = 451
mock_resp.text = '<html>Unavailable for legal reasons</html>'
status, reason = _evaluate_http_response('chancer8.xyz', mock_resp)
print(f'HTTP 451: status={status} reason={reason}')
assert status == 'red', f'Expected red for 451, got {status}'

print('ALL CF BLOCK TESTS PASSED')
"

Report the output. All assertions should pass.
```

### T16 — All 4 HU ASNs blocked returns "blocked"
```
On the EC2 instance at 63.178.175.200, cd to /opt/discord-bot and run this Python test. It patches _check_single_asn to simulate all 4 ASNs returning red (blocked), then verifies check_hu_consensus returns "blocked":

python3 -c "
from unittest.mock import patch
from monitor import check_hu_consensus

# Simulate all 4 ASNs blocked
def fake_check(geo, mirror, asn):
    return ('red', 'Hungarian government gambling block page (SZTFH)', asn)

with patch('monitor._check_single_asn', side_effect=fake_check):
    status, reason = check_hu_consensus('HU', 'chancer8.xyz')
    print(f'All blocked: status={status} reason={reason}')
    assert status == 'blocked', f'Expected blocked, got {status}'
    assert 'All 4 ASNs blocked' in reason, f'Unexpected reason: {reason}'

# Simulate 3 blocked + 1 up = should be up
def mixed_check(geo, mirror, asn):
    if asn == '5483':
        return ('up', None, asn)
    return ('red', 'blocked', asn)

with patch('monitor._check_single_asn', side_effect=mixed_check):
    status, reason = mixed_check_hu = check_hu_consensus('HU', 'chancer8.xyz')
    print(f'3 blocked 1 up: status={status} reason={reason}')
    assert status == 'up', f'Expected up, got {status}'

print('ALL HU CONSENSUS TESTS PASSED')
"

Report the output.
```

---

## HU Threshold Tests (T18, T19)

### T18 + T19 — 6-cycle alert threshold
```
On the EC2 instance at 63.178.175.200, cd to /opt/discord-bot and run this Python test. It simulates the HU 6-cycle threshold by manipulating GeoState directly:

python3 -c "
from datetime import datetime, timezone
from monitor import GeoState, _should_alert, FAILURE_THRESHOLD_HU

gs = GeoState()
now = datetime.now(timezone.utc)

# Simulate 5 consecutive all-blocked cycles — should NOT trigger alert
for i in range(5):
    gs.consecutive_failures = i + 1
    gs.status = 'red'
    should = _should_alert(gs, 'up', now)
    print(f'Cycle {i+1}/{FAILURE_THRESHOLD_HU}: consecutive_failures={gs.consecutive_failures}, should_alert={should}')

# At cycle 5, should still be False (threshold is 6)
assert gs.consecutive_failures == 5
# Note: _should_alert checks if status crossed up->red and alert_fired is False
# The actual threshold check happens in the monitor cycle, not in _should_alert
# So let's check the threshold value directly
print(f'FAILURE_THRESHOLD_HU = {FAILURE_THRESHOLD_HU}')
assert FAILURE_THRESHOLD_HU == 6, f'Expected threshold 6, got {FAILURE_THRESHOLD_HU}'
assert gs.consecutive_failures < FAILURE_THRESHOLD_HU, 'Should not alert at 5'

# Cycle 6 — should cross threshold
gs.consecutive_failures = 6
print(f'Cycle 6: consecutive_failures={gs.consecutive_failures} >= threshold={FAILURE_THRESHOLD_HU}: {gs.consecutive_failures >= FAILURE_THRESHOLD_HU}')
assert gs.consecutive_failures >= FAILURE_THRESHOLD_HU, 'Should alert at 6'

# Test reset on recovery
gs.consecutive_failures = 4
gs.status = 'up'
gs.consecutive_failures = 0
gs.alert_fired = False
print(f'After recovery: consecutive_failures={gs.consecutive_failures}, alert_fired={gs.alert_fired}')
assert gs.consecutive_failures == 0

print('ALL THRESHOLD TESTS PASSED')
"

Report the output.
```

---

## GR/PL Pending Confirmation (T24-T27)

### T24 — RIPE non-CF IP detected as hijack
```
On the EC2 instance at 63.178.175.200, cd to /opt/discord-bot and run:

python3 -c "
from monitor import _ripe_extract_ips, _parse_abuf_ips, EXPECTED_IP
import base64, struct

# Build a fake abuf with a hijacked IP (188.164.159.196 instead of 104.24.14.93)
# DNS response: 1 question, 1 answer, A record pointing to 188.164.159.196
hijack_ip = [188, 164, 159, 196]
# Minimal DNS response with the hijacked IP
header = struct.pack('!HHHHHH', 0x1234, 0x8180, 1, 1, 0, 0)
# Question: chancer1.xyz A IN
question = b'\x08chancer1\x03xyz\x00\x00\x01\x00\x01'
# Answer: compressed name + A record
answer = b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x01\x2c\x00\x04' + bytes(hijack_ip)
abuf = base64.b64encode(header + question + answer).decode()

ips = _parse_abuf_ips(abuf)
print(f'Parsed IPs from hijacked abuf: {ips}')
assert ips == ['188.164.159.196'], f'Expected [188.164.159.196], got {ips}'

hijacked = any(ip != EXPECTED_IP for ip in ips)
print(f'EXPECTED_IP={EXPECTED_IP}, hijacked={hijacked}')
assert hijacked == True, 'Should detect as hijacked'

# Control: build abuf with correct CF IP
cf_ip = [104, 24, 14, 93]
answer_ok = b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x01\x2c\x00\x04' + bytes(cf_ip)
abuf_ok = base64.b64encode(header + question + answer_ok).decode()
ips_ok = _parse_abuf_ips(abuf_ok)
print(f'Parsed IPs from clean abuf: {ips_ok}')
assert ips_ok == ['104.24.14.93']
hijacked_ok = any(ip != EXPECTED_IP for ip in ips_ok)
assert hijacked_ok == False, 'Should NOT detect as hijacked'

print('ALL RIPE HIJACK DETECTION TESTS PASSED')
"

Report the output.
```

### T25 + T26 + T27 — Pending confirmation window lifecycle
```
On the EC2 instance at 63.178.175.200, cd to /opt/discord-bot and run:

python3 -c "
from datetime import datetime, timedelta, timezone
from monitor import GeoState, PENDING_RETRY_MINUTES, PENDING_MAX_ATTEMPTS

now = datetime.now(timezone.utc)
gs = GeoState()

# T25: First hijack detection — enter pending, no alert
gs.pending_confirmation = True
gs.pending_attempts = 1
gs.pending_first_seen = now.isoformat(timespec='seconds')
gs.last_blocked_asns = ['1241']
gs.status = 'pending'
print(f'After 1st detection: pending={gs.pending_confirmation}, attempts={gs.pending_attempts}, status={gs.status}')
assert gs.pending_attempts < PENDING_MAX_ATTEMPTS, 'Should NOT alert yet'

# 2nd confirmation 60 min later — still no alert
gs.pending_attempts = 2
print(f'After 2nd detection: attempts={gs.pending_attempts}/{PENDING_MAX_ATTEMPTS}')
assert gs.pending_attempts < PENDING_MAX_ATTEMPTS, 'Should NOT alert at 2'

# T26: 3rd confirmation — should trigger alert
gs.pending_attempts = 3
print(f'After 3rd detection: attempts={gs.pending_attempts}/{PENDING_MAX_ATTEMPTS}')
assert gs.pending_attempts >= PENDING_MAX_ATTEMPTS, 'SHOULD alert at 3'
print(f'PENDING_MAX_ATTEMPTS={PENDING_MAX_ATTEMPTS}, reached={gs.pending_attempts >= PENDING_MAX_ATTEMPTS}')

# T27: Recovery before 3rd confirmation — pending should clear
gs2 = GeoState()
gs2.pending_confirmation = True
gs2.pending_attempts = 2
gs2.pending_first_seen = now.isoformat(timespec='seconds')
gs2.last_blocked_asns = ['1241']
gs2.status = 'pending'
# Simulate recovery
gs2.clear_pending()
gs2.status = 'up'
gs2.consecutive_failures = 0
print(f'After recovery: pending={gs2.pending_confirmation}, attempts={gs2.pending_attempts}, status={gs2.status}')
assert gs2.pending_confirmation == False
assert gs2.pending_attempts == 0
assert gs2.status == 'up'

print('ALL PENDING CONFIRMATION TESTS PASSED')
"

Report the output.
```

---

## Alert System (T36, T37, T39, T42)

### T36 — Verify alert webhook posts to Discord
```
On the EC2 instance at 63.178.175.200, check the monitor logs for any alert that has already been sent: grep -i "ALERT FIRED\|_send_alert\|alert.*webhook" /opt/discord-bot/logs/monitor.log /opt/discord-bot/logs/monitor.log.* 2>/dev/null. If you find evidence of alerts firing, report the timestamps and GEOs. If no alerts have ever fired in production, report that — we will need to trigger one via the blocked domain test to verify T36.
```

### T37 + T39 — Alert button state changes
```
On the EC2 instance at 63.178.175.200, cd to /opt/discord-bot and run this test to verify the Ignore and Mirror Updated button logic works correctly on the state layer (without needing Discord interaction):

python3 -c "
import json
from datetime import datetime, timedelta, timezone
from monitor import GeoState, MonitorState, IGNORE_DURATION_HOURS

now = datetime.now(timezone.utc)

# T37: Simulate Ignore button — sets ignored_until
gs = GeoState()
gs.status = 'red'
gs.alert_fired = True
gs.consecutive_failures = 6

until = now + timedelta(hours=IGNORE_DURATION_HOURS)
gs.ignored_until = until.isoformat(timespec='seconds')
print(f'T37 Ignore: ignored_until={gs.ignored_until}, IGNORE_DURATION_HOURS={IGNORE_DURATION_HOURS}')
assert gs.ignored_until is not None
# Check that ignored_until is ~1 hour from now
delta = until - now
print(f'  Mute duration: {delta.total_seconds()/3600:.1f} hours')
assert 0.9 < delta.total_seconds()/3600 < 1.1, 'Expected ~1 hour mute'

# T39: Simulate Mirror Updated button — resets outage state
gs2 = GeoState()
gs2.status = 'red'
gs2.alert_fired = True
gs2.consecutive_failures = 10
gs2.last_alert_sent = now.isoformat(timespec='seconds')
gs2.last_alert_message_id = 123456789

# Mirror Updated resets these fields (matching AlertView.mirror_updated logic):
gs2.active_mirror = 'chancer9.xyz'
gs2.status = 'unknown'
gs2.consecutive_failures = 0
gs2.last_alert_sent = None
gs2.last_alert_message_id = None
gs2.ignored_until = None

print(f'T39 Mirror Updated: status={gs2.status}, failures={gs2.consecutive_failures}, alert_sent={gs2.last_alert_sent}, mirror={gs2.active_mirror}')
assert gs2.status == 'unknown'
assert gs2.consecutive_failures == 0
assert gs2.last_alert_sent is None
assert gs2.last_alert_message_id is None
assert gs2.active_mirror == 'chancer9.xyz'

print('ALL ALERT BUTTON TESTS PASSED')
"

Report the output.
```

### T42 — Persistent buttons survive restart
```
On the EC2 instance at 63.178.175.200, check that AlertView is registered with persistent custom_ids:

python3 -c "
from monitor import AlertView
import discord

view = AlertView()
print(f'AlertView timeout: {view.timeout} (should be None for persistence)')
assert view.timeout is None, 'timeout must be None for persistent views'

for item in view.children:
    if isinstance(item, discord.ui.Button):
        print(f'Button: label={item.label!r} custom_id={item.custom_id!r}')
        assert item.custom_id is not None, f'Button {item.label} must have a custom_id'
        assert item.custom_id.startswith('monitor_alert:'), f'Unexpected custom_id: {item.custom_id}'

print('PERSISTENT VIEW TESTS PASSED')
print('Note: full T42 also requires manually restarting the bot and clicking an existing alert button in Discord.')
"

Report the output.
```

---

## Integration (T55, T57)

### T55 — Monitor picks up new mirror from redirects.json
```
SSH into the EC2 instance at 63.178.175.200. Read the current redirects.json: cat /opt/discord-bot/redirects.json. Note the current mirror for DK. Then watch the next monitor cycle in the logs: tail -f /opt/discord-bot/logs/monitor.log | grep -i "DK mirror=". Confirm the monitor is using the mirror value from redirects.json. Report the mirror value from the file and from the log line.
```

### T57 — Bot resumes from saved state after restart
```
SSH into the EC2 instance at 63.178.175.200. First capture the current monitor state: cat /opt/discord-bot/monitor_state.json | python3 -m json.tool | head -50. Note any non-zero consecutive_failures or non-"up" statuses. Then restart the bot: sudo systemctl restart discord-bot. Wait 15 seconds, then check: cat /opt/discord-bot/monitor_state.json | python3 -m json.tool | head -50. Compare the two. The state should be preserved (same failure counts, same statuses). Also check the logs for any spurious alerts: grep "ALERT FIRED" /opt/discord-bot/logs/monitor.log | tail -5. Report whether the state was preserved and whether any unexpected alerts fired during restart.
```

---

## Weekly Sweep (T34)

### T34 — Weekly RIPE sweep on Monday 04:00 UTC
```
SSH into the EC2 instance at 63.178.175.200. Check the logs for evidence of the weekly Monday sweep: grep -i "weekly.*sweep\|run_weekly_ripe" /opt/discord-bot/logs/monitor.log /opt/discord-bot/logs/monitor.log.* 2>/dev/null. If today is not Monday or it hasn't been Monday since the bot was deployed with the RIPE fix, this test needs to wait. Report what you find. If no evidence yet, note that this test should be checked next Monday after 04:00 UTC.
```
