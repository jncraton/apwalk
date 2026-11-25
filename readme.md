# AP Walk

Checks whether access point connectivity while walking around.

`apwalk.py` disconnects the selected Wi-Fi interface, creates temporary NetworkManager connection profiles, attempts to associate with matching BSSIDs, and logs IPv4 addresses. Running it interrupts Wifi connectivity.

## Requirements

- Python 3.10 or newer
- Linux with NetworkManager and `nmcli`
- The `iw` utility at `/usr/sbin/iw`
- A Wi-Fi interface supported by NetworkManager

## Input

The scan process is driven by a csv with a `mac_address` column. Additional columns are added the csv as a scan progresses:

- `bssid` — the visible BSSID matched to the listed MAC
- `test_status` — whether DHCP provided an address
- `tested_at` — the UTC time of the test
- `ipv4` — IPv4 addresses reported for the interface
