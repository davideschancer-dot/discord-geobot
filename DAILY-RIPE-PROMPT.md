# Daily Single-ASN RIPE Check for DK/FR/NO/AE — Implementation Prompt

## Priority tiers (drives every other decision in this doc)

| Tier | Geos | Detection target | Method | RIPE credit profile |
|---|---|---|---|---|
| 1 — real-time | HU | minutes | `hu_consensus` (Decodo per-ASN, every 10 min) | **zero RIPE** |
| 1 — real-time | GR, PL | hours | `ripe_reliable_asn` (twice daily 04/16 UTC + pending retries) | moderate |
| 3 — credit-conscious daily | DK, FR, NO, AE | up to 24h | new `ripe_daily_single_asn` (one ASN, 04 UTC) | **very low** |

**Real-time monitoring priority is HU, GR, and PL.** DK/FR/NO/AE are explicitly accepted as next-day detection in exchange for credit budget that keeps the Tier 1 geos working reliably.

## Motivating context — today's incident (2026-04-20)

A `/mirror-test` against `chancer1.xyz` in GR returned `medium confidence (Potentially Blocked)` because all four RIPE per-ASN measurements failed with `code: 102 — "not enough credit to schedule this measurement"`. Live GR/PL scheduled checks at 04/16 UTC and any DK/NO/FR/AE escalation paths are also no-ops until credits are topped up.

Root cause: `decodo_plus_ripe_confirm` in `monitor.py` fires a 4-ASN RIPE confirmation **every 10-minute cycle** any time Decodo reports "red" but the 4/4 consensus fails, because `gs.alert_fired` only flips true on a successful 4/4 match. Worst case = 16 RIPE measurements per 10 min across DK/FR/NO/AE = ~2.3k measurements/day burning credits with zero useful signal — and starving GR/PL when they actually need to fire.

The fix below removes that burn entirely and reserves the credit budget for the Tier 1 geos.

## PL — confirmed: stays on `ripe_reliable_asn`

PL is a Tier 1 real-time geo. Keep the existing twice-daily 04/16 UTC schedule + pending-retry windows. Costs ~5–15 RIPE measurements per day in steady state, more during pending windows. The twice-daily cadence is already 12× cheaper than the per-cycle escalation that caused today's burn, and PL is one of the larger gambling-block markets.

If credit budget later forces a downgrade, the change is a one-line `check_method` swap in `config.yaml` from `ripe_reliable_asn` to `ripe_daily_single_asn` plus making sure PL is included in `run_daily_single_asn_check`. Not in scope for this implementation.

## Context (decision rationale, retained)

For the Tier 3 geos the time-to-detect budget is wide (days), so we're dropping Decodo entirely for them and running a single-ASN RIPE DNS check once per day at 04:00 UTC. HU stays on `hu_consensus` (Decodo only, no RIPE). GR and PL stay on `ripe_reliable_asn` (04/16 UTC).

## Reliable ASNs to use — confirmed
- DK: AS3292 (TDC / Nuuday)
- NO: AS5381 (Telenor Norway) — *chosen over AS2116 which is Uninett/Sikt research network*
- FR: AS3215 (Orange France)
- AE: AS15412 (FLAG/Etisalat) — *Etisalat is the state ISP most likely to enforce state blocks*

## Changes

1. `config.yaml`
   - Change `check_method` for DK, FR, NO, AE from `decodo_plus_ripe_confirm` to a new method `ripe_daily_single_asn`.
   - Keep the existing `asns:` arrays (used elsewhere); add nothing new there.

2. `monitor.py`
   - Add a `RELIABLE_ASN` entry for DK/FR/NO/AE:
     ```python
     RELIABLE_ASN = {
         "GR": "1241",
         "PL": "5617",
         "DK": "3292",
         "NO": "5381",
         "FR": "3215",
         "AE": "15412",
     }
     ```
   - Add a constant `DAILY_RIPE_HOUR = 4` (UTC).
   - In `run_monitor_cycle`, add `ripe_daily_single_asn` to the list of check methods that are skipped by the regular cycle (next to `ripe_reliable_asn`).
   - Write a new scheduled runner `run_daily_single_asn_check(bot)` that iterates every geo with `check_method == "ripe_daily_single_asn"` and monitor:true, loads its mirror from redirects.json, and fires exactly one `ripe_check_asn(mirror, cc, RELIABLE_ASN[cc])`. If `hijacked` is true, call `_send_alert` directly with `status="red"` and `reason="Daily RIPE single-ASN check: AS{asn} hijacked, IPs={ips}"`, and set `gs.alert_fired=True` + `gs.last_alert_sent=now` + `gs.status="red"`. If hijacked is false, clear outage state. Save MonitorState at the end.
   - In the `@tasks.loop` monitor loop, add a tracker `_last_daily_ripe_hour` (same pattern as `_last_ripe_hour`) and fire `run_daily_single_asn_check(bot)` once when UTC hour == `DAILY_RIPE_HOUR` and we haven't fired this hour yet. Reset when we leave the hour.
   - Delete the `decodo_plus_ripe_confirm` branch from `run_monitor_cycle` (lines ~1030–1052). Leave `_run_4asn_confirm` in place — still used by the weekly sweep for these geos (see next item).
   - Weekly sweep: the daily check supersedes the weekly sweep for these geos. Remove DK/NO/FR/AE from `run_weekly_ripe_sweep` OR disable it entirely if no geo needs it. Document whichever you choose.
   - `CONFIRM_ASNS` can stay in the file for now (weekly sweep still references it) but add a comment that it's unused for daily checks.

3. `README.md` and `GEO-MONITOR-SPEC.md`
   - Update the check-method table/section to remove `decodo_plus_ripe_confirm` and add `ripe_daily_single_asn`.
   - Note the 04:00 UTC daily schedule and that these geos sacrifice same-day detection for credit efficiency.
   - Note HU explicitly uses zero RIPE credits.

4. State migration
   - Existing `MonitorState` entries for DK/FR/NO/AE may have `consecutive_failures > 0` and `alert_fired` set from the Decodo path. On first run under the new method, these fields are harmless — the daily runner doesn't read them — but clean them up via `gs.clear_outage()` once on startup if `check_method == "ripe_daily_single_asn"` and `consecutive_failures > 0`. One-shot migration, not recurring.

5. **Credit guard rails (REQUIRED — see "Graceful degradation" section below)**
   - Add `_ripe_credits_available()` helper and `RIPE_CREDIT_FLOOR` constant in `monitor.py`. Cache the credit balance for 60s.
   - Gate every measurement-creating entry point on the floor: `_run_ripe_gr_pl_once`, `run_daily_single_asn_check`, `run_weekly_ripe_sweep` (if retained), and the RIPE path in `/mirror-test`.
   - Add `monitor.ripe_credit_floor: 50` to `config.yaml` so it's tunable.
   - Add `"all_errored"` to the `ripe_check_per_asn` return dict.
   - Update `/mirror-test` (`discord_bot.py`) to set `ripe_failed = True` when `all_errored` is true, so the verdict falls through the existing "RIPE unavailable — Decodo only" branch instead of treating zero hijacks as a clean signal.
   - Update `_run_ripe_gr_pl_once` to short-circuit (no state mutation, no `clear_outage`) when `all_errored` is true.

## Deployment
Local first: run `python discord_bot.py`, wait for 04:00 UTC or manually trigger by temporarily setting `DAILY_RIPE_HOUR` to the current hour, confirm one RIPE measurement per geo and no alerts if sites are up.

Verify the credit guard rails before pushing:
- Temporarily set `monitor.ripe_credit_floor` very high (e.g. 999999) in `config.yaml`, run `/mirror-test url:chancer1.xyz geo:GR`. Expect verdict to fall through to the "RIPE unavailable" path with the DNS field labelled "API errors / no data" (or "low credit" depending on log line) and **no** false-clean green verdict. Revert the config change.
- Confirm the live monitor logs `ripe_skipped reason=low_credit` for any GR/PL scheduled tick that runs while the floor is exceeded, and that `_run_ripe_gr_pl_once` does not call `clear_outage()` on those ticks.

Then EC2: push via `deploy.sh`. Verify in the EC2 logs that at the next 04:00 UTC window exactly 4 RIPE measurements fire (one per geo) and no further RIPE activity happens in the monitor cycle across the day for these geos. Confirm `_ripe_credits_available()` is being called and logged once per gated entry point.

## Expected credit impact

**Per-geo daily measurement count** (each measurement = 5 probes by default):

| Geo | Before (Decodo+RIPE confirm churn) | After (Tier 3 daily) | Change |
|---|---|---|---|
| HU | 0 (Decodo-only, no RIPE) | 0 | unchanged |
| GR | ~5–15 (twice-daily + pending retries) | ~5–15 | unchanged |
| PL | ~5–15 (twice-daily + pending retries) | ~5–15 | unchanged |
| DK | up to ~580 (4-ASN every 10 min when Decodo red) | 1 | **~99.8% drop** |
| NO | up to ~580 | 1 | **~99.8% drop** |
| FR | up to ~580 | 1 | **~99.8% drop** |
| AE | up to ~580 | 1 | **~99.8% drop** |
| **Total** | up to **~2.3k–11.5k/day** | **~14–34/day** | **~100×–800× reduction** |

Probe-sample totals are ~5× the measurement count. The wide range on the "before" numbers reflects how many of DK/NO/FR/AE were actively in the Decodo-red-but-not-confirmed loop at any given time.

## Graceful degradation when credits run low (REQUIRED — implement alongside the daily-check work)

Today's incident exposed two silent-failure modes. Both must be fixed in the same change-set.

### 1. Pre-flight credit check (REQUIRED)

Before any RIPE measurement creation, query the credit balance and skip the cycle if it would push us below a safety floor.

- Endpoint: `GET https://atlas.ripe.net/api/v2/credits/` with `Authorization: Key <RIPE_ATLAS_API_KEY>`. Returns JSON including `current_balance` (int).
- Add a helper in `monitor.py`:
  ```python
  RIPE_CREDIT_FLOOR = int(MONITOR_CFG.get("ripe_credit_floor", 50))

  def _ripe_credits_available() -> int | None:
      """Return current RIPE credit balance, or None on error."""
      if not RIPE_ATLAS_API_KEY:
          return None
      try:
          resp = requests.get(
              f"{RIPE_API_BASE}/credits/",
              headers={"Authorization": f"Key {RIPE_ATLAS_API_KEY}"},
              timeout=10,
          )
          if resp.status_code == 200:
              return int(resp.json().get("current_balance", 0))
      except Exception as e:
          log.warning("Failed to read RIPE credit balance: %s", e)
      return None
  ```
- Gate every entry point that schedules measurements (`_run_ripe_gr_pl_once`, `_run_4asn_confirm` if retained, `run_daily_single_asn_check`, `ripe_check_asn` called from `/mirror-test`):
  ```python
  credits = _ripe_credits_available()
  if credits is not None and credits < RIPE_CREDIT_FLOOR:
      log.warning("ripe_skipped reason=low_credit balance=%d floor=%d cc=%s",
                  credits, RIPE_CREDIT_FLOOR, cc)
      return  # or surface to caller
  ```
- Floor: configurable via `monitor.ripe_credit_floor` in `config.yaml`, default `50` — enough headroom for one GR or PL twice-daily cycle (~25–50 credits) without going negative.
- The credit endpoint is itself free, but cache the result for 60s so back-to-back calls in one cycle don't all hit the API.

### 2. Distinguish "RIPE clean" from "RIPE unavailable" (REQUIRED)

`ripe_check_per_asn` already returns `error` per ASN; today both `/mirror-test` verdict logic and the live monitor treat `hijacked_asns: []` as clean even when every measurement errored. Fix: when **all** per-ASN entries have a non-null `error`, surface that as an explicit `ripe_unavailable` signal upstream.

- In `ripe_check_per_asn`, add `"all_errored": all(r["error"] for r in per_asn.values())` to the returned dict.
- `/mirror-test` (`discord_bot.py`): if `ripe_summary["all_errored"]`, set `ripe_failed = True` so the existing "RIPE unavailable — Decodo only" branch is hit. Label the DNS field "API errors / no data" (include the first error string for debug).
- Live monitor `_run_ripe_gr_pl_once` (`monitor.py`): if `summary["all_errored"]`, do NOT call `clear_outage()`, do NOT increment pending state, do NOT alter `last_blocked_asns`. Just log `ripe_unavailable cc=<X> reason=<first_error>` and return. An empty `hijacked_asns` list with all-errors must never silently clear a real pending-confirmation window.
- New daily runner `run_daily_single_asn_check`: if the single `ripe_check_asn` call returns `error`, log `ripe_unavailable cc=<X>` and skip — do not flip status to clean.

These two guard rails together prevent both the silent burn (mode #1) and the silent false-clean (mode #2) failure modes.

## What NOT to change
- HU `hu_consensus` — untouched. Tier 1 real-time, zero RIPE.
- GR `ripe_reliable_asn` at 04/16 UTC — untouched. Tier 1 real-time.
- PL `ripe_reliable_asn` at 04/16 UTC — **untouched pending the open question above.**
- Pending-confirmation logic for GR/PL — untouched.
- `_send_alert`, `MonitorState`, `AlertView` — untouched. The daily check uses the same alert path as everything else.
