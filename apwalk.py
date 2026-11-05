#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///

import argparse
import csv
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def run(*args, timeout=20):
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def split_nmcli_fields(line):
    """Split colon-delimited nmcli output, preserving escaped colons."""
    fields = []
    current = []
    escaped = False

    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)

    fields.append("".join(current))
    return fields


def normalize_mac(value):
    return value.strip().lower().replace("-", ":")


def suffix(mac):
    """Return the last three octets of a MAC address."""
    parts = normalize_mac(mac).split(":")
    if len(parts) != 6:
        return None
    return ":".join(parts[-3:])


def scan_bssids(interface, ssid):
    result = run(
        "nmcli", "--terse", "--escape", "yes",
        "--fields", "BSSID,SSID",
        "device", "wifi", "list",
        "ifname", interface,
        "--rescan", "yes",
        timeout=30,
    )

    if result.returncode != 0:
        print(f"Wi-Fi scan error: {result.stderr.strip()}", file=sys.stderr)
        return set()

    found = set()
    for line in result.stdout.splitlines():
        fields = split_nmcli_fields(line)
        if len(fields) >= 2 and fields[1] == ssid:
            bssid = normalize_mac(fields[0])
            if suffix(bssid):
                found.add(bssid)

    return found


def get_ipv4_addresses(interface):
    result = run(
        "nmcli", "--terse", "--fields", "IP4.ADDRESS",
        "device", "show", interface,
    )
    if result.returncode != 0:
        return []

    addresses = []
    for line in result.stdout.splitlines():
        if line.startswith("IP4.ADDRESS") and ":" in line:
            address = line.split(":", 1)[1].strip()
            if address:
                addresses.append(address)
    return addresses


def save_csv(path, rows, fieldnames):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)


def main():
    parser = argparse.ArgumentParser(
        description="Match listed AP MACs to visible BSSIDs by their last three octets."
    )
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("--ssid", required=True)
    parser.add_argument("--interface", required=True, help="Wi-Fi interface, e.g. wlan0")
    parser.add_argument("--dhcp-wait", type=int, default=20)
    parser.add_argument("--scan-interval", type=int, default=5)
    args = parser.parse_args()

    if not args.csv_file.exists():
        parser.error(f"CSV file not found: {args.csv_file}")

    with args.csv_file.open(newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or "mac_address" not in reader.fieldnames:
            parser.error("CSV must have a header containing a 'mac_address' column")
        fieldnames = list(reader.fieldnames)
        print(fieldnames)
        rows = list(reader)

    for column in ("bssid", "test_status", "tested_at", "ipv4"):
        if column not in fieldnames:
            fieldnames.append(column)

    for row in rows:
        row["mac_address"] = normalize_mac(row.get("mac_address", ""))
        row.setdefault("bssid", "")
        row.setdefault("test_status", "")
        row.setdefault("tested_at", "")
        row.setdefault("ipv4", "")

    # Group records by suffix so duplicate AP MAC suffixes are detected.
    rows_by_suffix = {}
    for index, row in enumerate(rows):
        mac_suffix = suffix(row["mac_address"])
        if mac_suffix and not row["test_status"].strip():
            rows_by_suffix.setdefault(mac_suffix, []).append(index)

    pending = set(rows_by_suffix)
    if not pending:
        print("No rows with a valid MAC and blank status.")
        return

    print(f"Testing unresolved APs for SSID {args.ssid!r}. Press Ctrl+C to stop.")

    try:
        while pending:
            visible = scan_bssids(args.interface, args.ssid)
            visible_by_suffix = {}
            for bssid in visible:
                visible_by_suffix.setdefault(suffix(bssid), set()).add(bssid)

            tested_this_cycle = False

            for mac_suffix in sorted(pending):
                matching_rows = rows_by_suffix[mac_suffix]
                matching_bssids = visible_by_suffix.get(mac_suffix, set())

                if len(matching_rows) != 1:
                    print(f"Ambiguous AP MAC suffix {mac_suffix}: multiple CSV rows; skipping.")
                    continue

                if len(matching_bssids) == 0:
                    continue

                row = rows[matching_rows[0]]
                bssid = next(iter(matching_bssids))
                print(f"Testing AP MAC {row['mac_address']} via BSSID {bssid} ...")

                run("nmcli", "device", "disconnect", args.interface, timeout=10)
                time.sleep(1)

                bssid_upper = bssid.upper()
                profile = "ap-test-" + bssid_upper.replace(":", "-")

                # Remove a leftover profile from an interrupted previous run.
                run("nmcli", "connection", "delete", "id", profile, timeout=10)

                addresses = []

                try:
                    created = run(
                        "nmcli", "connection", "add",
                        "type", "wifi",
                        "ifname", args.interface,
                        "con-name", profile,
                        "ssid", args.ssid,
                        "wifi.bssid", bssid_upper,
                        "ipv4.method", "auto",
                        "ipv6.method", "disabled",
                        timeout=20,
                    )

                    if created.returncode == 0:
                        activated = run(
                            "nmcli", "connection", "up",
                            "id", profile,
                            "ifname", args.interface,
                            timeout=args.dhcp_wait + 15,
                        )

                        if activated.returncode == 0:
                            time.sleep(5)
                            link = run("/usr/sbin/iw", "dev", args.interface, "link")
                            print(link.stdout)
                            addresses = get_ipv4_addresses(args.interface)
                        else:
                            print(f"  Could not activate pinned profile: {activated.stderr.strip()}")
                    else:
                        print(f"  Could not create pinned profile: {created.stderr.strip()}")

                except subprocess.TimeoutExpired:
                    print("  Connection attempt timed out.")

                # Remove the temporary profile and disconnect before the next test.
                run("nmcli", "connection", "down", "id", profile, timeout=10)
                run("nmcli", "connection", "delete", "id", profile, timeout=10)

                row["bssid"] = bssid
                row["test_status"] = (
                    "dhcp_obtained" if addresses else "no_dhcp_observed"
                )
                row["tested_at"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                row["ipv4"] = ";".join(addresses)

                save_csv(args.csv_file, rows, fieldnames)
                pending.discard(mac_suffix)
                tested_this_cycle = True

                print(
                    f"  {row['test_status']}"
                    + (f": {', '.join(addresses)}" if addresses else "")
                )

                run("nmcli", "device", "disconnect", args.interface, timeout=10)
                time.sleep(2)

            if pending:
                if not tested_this_cycle:
                    print(f"No unique matches yet; {len(pending)} AP(s) remain.")
                time.sleep(args.scan_interval)

    except KeyboardInterrupt:
        print("\nStopped. Completed results are saved in the CSV.")

    save_csv(args.csv_file, rows, fieldnames)


if __name__ == "__main__":
    main()
