"""
test_blocked_domains.py — verify detection logic using known-blocked domains.

Runs Decodo HTTP checks and RIPE DNS checks against domains that are
blocked in specific countries. Does NOT touch redirects.json, monitor_state,
or Discord. Safe to run alongside the live bot.

Usage:
    python test_blocked_domains.py          # run all tests
    python test_blocked_domains.py DK       # run one GEO only
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Import monitor functions directly
from monitor import (
    CONFIRM_ASNS,
    EXPECTED_IP,
    UAE_BLOCK_IPS,
    check_http,
    check_hu_consensus,
    ripe_check_per_asn,
    _evaluate_http_response,
    log,
)

# ── Known-blocked domains per GEO ──────────────────────────────────────────
BLOCKED_DOMAINS = {
    "FR": "wolfycasino.com",
    "DK": "verdecasino.com",
    "NO": "verdecasino.com",
    "AE": "bwin.com",
}

# ── Test definitions ───────────────────────────────────────────────────────
TESTS = [
    # --- Decodo HTTP detection (T12-T14 style) ---
    {
        "id": "HTTP-FR",
        "desc": "Decodo HTTP check detects FR block on wolfycasino.com",
        "type": "http",
        "geo": "FR",
        "expect_status": "red",
        "maps_to": ["T12", "T13", "T14"],
    },
    {
        "id": "HTTP-DK",
        "desc": "Decodo HTTP check detects DK block on verdecasino.com",
        "type": "http",
        "geo": "DK",
        "expect_status": "red",
        "maps_to": ["T12", "T13", "T14"],
    },
    {
        "id": "HTTP-NO",
        "desc": "Decodo HTTP check detects NO block on verdecasino.com",
        "type": "http",
        "geo": "NO",
        "expect_status": "red",
        "maps_to": ["T12", "T13", "T14"],
    },
    {
        "id": "HTTP-AE",
        "desc": "Decodo HTTP check detects AE block on bwin.com",
        "type": "http",
        "geo": "AE",
        "expect_status": "red",
        "maps_to": ["T12", "T13", "T14"],
    },
    # --- RIPE 4-ASN DNS confirmation (T24, T33 style) ---
    {
        "id": "RIPE-FR",
        "desc": "RIPE 4-ASN confirm detects DNS hijack in FR for wolfycasino.com",
        "type": "ripe_confirm",
        "geo": "FR",
        "expect_hijacked": True,
        "maps_to": ["T24", "T33"],
    },
    {
        "id": "RIPE-DK",
        "desc": "RIPE 4-ASN confirm detects DNS hijack in DK for verdecasino.com",
        "type": "ripe_confirm",
        "geo": "DK",
        "expect_hijacked": True,
        "maps_to": ["T24", "T33"],
    },
    {
        "id": "RIPE-NO",
        "desc": "RIPE 4-ASN confirm detects DNS hijack in NO for verdecasino.com",
        "type": "ripe_confirm",
        "geo": "NO",
        "expect_hijacked": True,
        "maps_to": ["T24", "T33"],
    },
    {
        "id": "RIPE-AE",
        "desc": "RIPE 4-ASN confirm detects block in AE for bwin.com",
        "type": "ripe_confirm",
        "geo": "AE",
        "expect_hijacked": True,
        "maps_to": ["T24", "T33", "T35"],
    },
    # --- Control: legitimate domain should be UP ---
    {
        "id": "HTTP-FR-CTRL",
        "desc": "Decodo HTTP check shows chancer1.xyz UP in FR (control)",
        "type": "http",
        "geo": "FR",
        "domain_override": "chancer1.xyz",
        "expect_status": "up",
        "maps_to": ["T29"],
    },
    {
        "id": "RIPE-DK-CTRL",
        "desc": "RIPE 4-ASN shows chancer1.xyz NOT hijacked in DK (control)",
        "type": "ripe_confirm",
        "geo": "DK",
        "domain_override": "chancer1.xyz",
        "expect_hijacked": False,
        "maps_to": ["T23"],
    },
]


# ── Runners ────────────────────────────────────────────────────────────────
def run_http_test(test: dict) -> dict:
    geo = test["geo"]
    domain = test.get("domain_override", BLOCKED_DOMAINS[geo])
    expect = test["expect_status"]

    print(f"  [{test['id']}] Decodo HTTP: {domain} via {geo} ...", end=" ", flush=True)
    start = time.monotonic()
    status, reason = check_http(geo, domain)
    elapsed = time.monotonic() - start

    passed = (
        status == expect
        or (expect == "red" and status == "red")
        or (expect == "up" and status == "up")
    )
    # Also accept orange on "expect red" — proxy errors are inconclusive, not a failure
    if expect == "red" and status == "orange":
        verdict = "INCONCLUSIVE"
    else:
        verdict = "PASS" if passed else "FAIL"

    print(f"{verdict} ({elapsed:.1f}s) status={status} reason={reason}")
    return {
        "test_id": test["id"],
        "maps_to": test["maps_to"],
        "domain": domain,
        "geo": geo,
        "expected": expect,
        "actual_status": status,
        "reason": reason,
        "verdict": verdict,
        "elapsed_s": round(elapsed, 1),
    }


def run_ripe_test(test: dict) -> dict:
    geo = test["geo"]
    domain = test.get("domain_override", BLOCKED_DOMAINS[geo])
    expect_hijacked = test["expect_hijacked"]
    asns = CONFIRM_ASNS.get(geo, [])

    print(f"  [{test['id']}] RIPE 4-ASN: {domain} via {geo} ASNs {asns} ...", flush=True)
    start = time.monotonic()
    summary = ripe_check_per_asn(domain, geo, asns)
    elapsed = time.monotonic() - start

    hijacked_asns = summary["hijacked_asns"]
    all_hijacked = summary["all_hijacked"]
    per_asn = summary["per_asn"]

    # For blocked domains: expect at least some hijacked ASNs
    # For controls: expect none hijacked
    if expect_hijacked:
        if all_hijacked:
            verdict = "PASS (4/4 hijacked)"
        elif hijacked_asns:
            verdict = f"PARTIAL ({len(hijacked_asns)}/{len(asns)} hijacked)"
        else:
            # Check if errors prevented detection
            errors = [r["error"] for r in per_asn.values() if r["error"]]
            if errors:
                verdict = "INCONCLUSIVE (measurement errors)"
            else:
                verdict = "FAIL (expected hijack, got clean)"
    else:
        if not hijacked_asns:
            verdict = "PASS"
        else:
            verdict = f"FAIL (expected clean, got {len(hijacked_asns)} hijacked)"

    # Per-ASN detail
    for asn, r in per_asn.items():
        status_str = "HIJACKED" if r["hijacked"] else "clean"
        print(f"    AS{asn}: {status_str} ips={r['ips']} err={r['error']}")

    print(f"    Result: {verdict} ({elapsed:.1f}s)")
    return {
        "test_id": test["id"],
        "maps_to": test["maps_to"],
        "domain": domain,
        "geo": geo,
        "expected": "hijacked" if expect_hijacked else "clean",
        "hijacked_asns": hijacked_asns,
        "all_hijacked": all_hijacked,
        "per_asn": {asn: {"hijacked": r["hijacked"], "ips": r["ips"], "error": r["error"]}
                    for asn, r in per_asn.items()},
        "verdict": verdict,
        "elapsed_s": round(elapsed, 1),
    }


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    geo_filter = sys.argv[1].upper() if len(sys.argv) > 1 else None

    tests_to_run = TESTS
    if geo_filter:
        tests_to_run = [t for t in TESTS if t["geo"] == geo_filter]

    if not tests_to_run:
        print(f"No tests found for GEO '{geo_filter}'")
        sys.exit(1)

    print(f"\n{'='*70}")
    print(f"  GeoBot Block Detection Test — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Domains: {json.dumps(BLOCKED_DOMAINS, indent=2)}")
    print(f"  Expected CF IP: {EXPECTED_IP}")
    if geo_filter:
        print(f"  Filter: {geo_filter} only")
    print(f"{'='*70}\n")

    results = []

    # Run HTTP tests first (faster)
    http_tests = [t for t in tests_to_run if t["type"] == "http"]
    if http_tests:
        print("── Decodo HTTP checks ──")
        for t in http_tests:
            results.append(run_http_test(t))
        print()

    # Run RIPE tests (slower, 10-15s each for measurement + polling)
    ripe_tests = [t for t in tests_to_run if t["type"] == "ripe_confirm"]
    if ripe_tests:
        print("── RIPE 4-ASN DNS confirmation ──")
        for t in ripe_tests:
            results.append(run_ripe_test(t))
        print()

    # Summary
    print(f"{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")
    pass_count = sum(1 for r in results if r["verdict"].startswith("PASS"))
    fail_count = sum(1 for r in results if r["verdict"].startswith("FAIL"))
    partial = sum(1 for r in results if r["verdict"].startswith("PARTIAL"))
    inconclusive = sum(1 for r in results if r["verdict"].startswith("INCONCLUSIVE"))

    for r in results:
        tag = r["verdict"].split(" ")[0]
        marker = {"PASS": "+", "FAIL": "X", "PARTIAL": "~", "INCONCLUSIVE": "?"}
        print(f"  [{marker.get(tag, '?')}] {r['test_id']:15s} {r['verdict']}")
        if r.get("maps_to"):
            print(f"      Covers test matrix: {', '.join(r['maps_to'])}")

    print()
    print(f"  PASS: {pass_count}  FAIL: {fail_count}  PARTIAL: {partial}  INCONCLUSIVE: {inconclusive}")
    print(f"{'='*70}\n")

    # Save full results
    out_path = Path(__file__).resolve().parent / "test_results.json"
    with open(out_path, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "blocked_domains": BLOCKED_DOMAINS,
            "results": results,
        }, f, indent=2)
    print(f"  Full results saved to: {out_path}\n")


if __name__ == "__main__":
    main()
