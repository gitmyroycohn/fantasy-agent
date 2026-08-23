"""
fp_probe.py — verify FantasyPros' NFL data against reality instead of the
guessed field names currently in fantasypros/client.py and
agent/football_decisions.py::_fp_nfl_rankings_by_name(), and separately probe
the new MCP endpoint at https://api.fantasypros.com/mcp that Christopher
flagged on 2026-08-23.

Same pattern as dump_samples.py: run locally (this machine has real internet
access; the Cowork sandbox that wrote this script does not), then share the
samples/ output back with Claude.

Run:  python fp_probe.py
Writes files into samples/ — share them back with Claude.
"""

import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import requests

from config.settings import FANTASYPROS_API_KEY

OUT = os.path.join(os.path.dirname(__file__), "samples")
os.makedirs(OUT, exist_ok=True)

REST_BASE = "https://api.fantasypros.com/public/v2/json"
MCP_URL = "https://api.fantasypros.com/mcp"


def save(name: str, content: str):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  wrote {path} ({len(content)} chars)")


def check_key():
    if not FANTASYPROS_API_KEY:
        print("FANTASYPROS_API_KEY is not set (config/settings.py / .env) — "
              "nothing to probe. Set it and re-run.")
        sys.exit(1)
    masked = FANTASYPROS_API_KEY[:4] + "…" + FANTASYPROS_API_KEY[-2:] \
        if len(FANTASYPROS_API_KEY) > 8 else "***"
    print(f"Using FANTASYPROS_API_KEY: {masked} (never printed in full)\n")


def probe_rest_nfl():
    """Hit the existing REST v2 NFL endpoints already coded in
    fantasypros/client.py and capture the *real* field names, so
    _fp_nfl_rankings_by_name()'s multi-key guessing can be tightened to the
    confirmed shape and the "unverified" caveat can be removed."""
    print("[1] REST v2 /nfl consensus-rankings + projections...")
    session = requests.Session()
    session.headers.update({"x-api-key": FANTASYPROS_API_KEY, "Accept": "application/json"})

    calls = [
        ("consensus_rankings_ww.json", "/nfl/2026/consensus-rankings",
         {"position": "ALL", "scoring": "PPR", "type": "WW"}),
        ("consensus_rankings_ros.json", "/nfl/2026/consensus-rankings",
         {"position": "ALL", "scoring": "PPR", "type": "ROS"}),
        ("projections.json", "/nfl/2026/projections",
         {"position": "ALL", "scoring": "PPR"}),
    ]
    for fname, path, params in calls:
        url = f"{REST_BASE}{path}"
        try:
            r = session.get(url, params=params, timeout=15)
            print(f"    GET {path} {params} -> HTTP {r.status_code}")
            body = r.text
            save(fname, body[:60000])
            try:
                d = r.json()
                players = d.get("players", []) or d.get("player", [])
                print(f"      top-level keys: {list(d.keys())[:10]}")
                print(f"      num players: {len(players)}")
                if players:
                    print(f"      first player keys: {list(players[0].keys())}")
            except Exception as e:
                print(f"      (couldn't parse as JSON: {e})")
        except Exception as e:
            print(f"    GET {path} -> ERROR {e}")
            save(fname, f"ERROR: {e}")


def probe_mcp():
    """The MCP endpoint Christopher flagged returns HTTP 401 from a sandbox
    with no auth — this sends a real MCP 'initialize' request (Streamable
    HTTP transport per the MCP spec) with the same API key, trying both
    x-api-key and Authorization: Bearer, to see which (if either) it
    accepts and what it advertises back (server info, capabilities, tool
    list via a follow-up tools/list call if initialize succeeds)."""
    print("\n[2] MCP endpoint probe (api.fantasypros.com/mcp)...")
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "fantasy-agent-probe", "version": "0.1"},
        },
    }
    headers_common = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    auth_variants = [
        ("x-api-key", {"x-api-key": FANTASYPROS_API_KEY}),
        ("bearer", {"Authorization": f"Bearer {FANTASYPROS_API_KEY}"}),
    ]

    report_lines = []
    session_id = None
    for label, auth_headers in auth_variants:
        headers = {**headers_common, **auth_headers}
        try:
            r = requests.post(MCP_URL, headers=headers, json=init_body, timeout=15)
            line = (f"{label}: HTTP {r.status_code}, "
                     f"content-type={r.headers.get('content-type')}, "
                     f"mcp-session-id={r.headers.get('mcp-session-id')}")
            print(f"    {line}")
            report_lines.append(line)
            report_lines.append(f"  body: {r.text[:2000]}")
            if r.status_code == 200 and not session_id:
                session_id = r.headers.get("mcp-session-id")
        except Exception as e:
            line = f"{label}: ERROR {e}"
            print(f"    {line}")
            report_lines.append(line)

    # If either auth variant produced a session, try listing tools —
    # this is the part that actually tells us if it's worth integrating.
    if session_id:
        for label, auth_headers in auth_variants:
            headers = {**headers_common, **auth_headers, "mcp-session-id": session_id}
            list_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            try:
                r = requests.post(MCP_URL, headers=headers, json=list_body, timeout=15)
                line = f"{label} tools/list: HTTP {r.status_code}"
                print(f"    {line}")
                report_lines.append(line)
                report_lines.append(f"  body: {r.text[:4000]}")
            except Exception as e:
                report_lines.append(f"{label} tools/list: ERROR {e}")

    save("fp_mcp_probe_report.txt", "\n".join(report_lines))


def main():
    check_key()
    probe_rest_nfl()
    probe_mcp()
    print("\nDone. Share the samples/ folder contents (or just paste the "
          "printed output above) back with Claude.")


if __name__ == "__main__":
    main()
