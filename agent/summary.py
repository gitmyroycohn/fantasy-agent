"""
Phone-friendly TL;DR summary block — prepended to latest_output.md.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _random_image_block() -> str:
    """Fetch a random historic baseball image for the daily header.
    Returns a markdown image block, or empty string on failure."""
    try:
        from mlb.images import random_historic_image
        img = random_historic_image()
        if not img:
            return ""
        title = img.get("title", "Historic Baseball")
        url   = img.get("url", "")
        src   = img.get("source", "")
        date  = img.get("date", "")
        date_str = f" ({date})" if date else ""
        return (
            f"\n![{title}]({url})\n"
            f"*{title}{date_str} — {src}*\n"
        )
    except Exception:
        return ""


def format_tldr(results: list[dict]) -> str:
    """
    Build a short summary block from the list of per-league result dicts
    returned by run_decisions().

    Each dict has keys: league, format, actions, and optionally matchup.
    """
    now = datetime.now(_ET).strftime("%a %b %-d, %Y  %-I:%M %p ET")
    lines = [
        "=" * 48,
        f"  FANTASY AGENT  --  {now}",
        "=" * 48,
        _random_image_block(),
    ]

    for res in results:
        league_name = res.get("league", "?")
        fmt         = res.get("format", "")
        actions     = res.get("actions", [])

        lines.append("")
        if "H2H" in fmt:
            lines.append(f"[ PINS & PILLS  |  H2H Categories ]")
        else:
            lines.append(f"[ CASEY STENGEL  |  NL-Only Roto ]")

        for action in actions:
            atype = action.get("type", "")

            # --- H2H matchup summary ---
            if atype == "matchup_summary":
                summary = action.get("summary", "")
                pri     = action.get("priority_cats", [])
                lines.append(f"  Matchup : {summary}")
                if pri:
                    # show only the top 4 to keep it short
                    top = ", ".join(pri[:4])
                    more = f" +{len(pri)-4} more" if len(pri) > 4 else ""
                    lines.append(f"  Target  : {top}{more}")

            # --- Roto summary ---
            elif atype == "roto_summary":
                summary  = action.get("summary", "")
                weak     = action.get("weak_cats", [])
                lines.append(f"  Standings: {summary}")
                if weak:
                    lines.append(f"  Weakest  : {', '.join(weak[:4])}")

            # --- Streaming SP ---
            elif atype == "streaming_sp":
                recs = action.get("recommendations", [])
                note = action.get("note", "")
                if recs:
                    r = recs[0]  # top pick only for TL;DR
                    tag = " [2-START]" if r.get("starts", 1) >= 2 else ""
                    lines.append(
                        f"  Stream SP: {r['player']} ({r['team']}){tag}"
                        f"  ERA {r.get('era','?')}  K/9 {r.get('k9','?')}"
                    )
                    if note:
                        lines.append(f"  ** {note} **")
                    if len(recs) > 1:
                        others = ", ".join(r2['player'] for r2 in recs[1:3])
                        lines.append(f"  Also    : {others}")

            # --- Waiver adds ---
            elif atype == "waiver_adds":
                recs = action.get("recommendations", [])
                if recs:
                    top = recs[0]
                    pos = "/".join(top.get("positions", []))
                    cats = ", ".join(top.get("helps_cats", []))
                    lines.append(
                        f"  Top Add  : {top['player']} ({top['team']}) [{pos}]"
                        + (f"  helps {cats}" if cats else "")
                    )
                    if len(recs) > 1:
                        others = ", ".join(
                            f"{r['player']} ({'/'.join(r.get('positions',[]))})"
                            for r in recs[1:3]
                        )
                        lines.append(f"  Also     : {others}")

            # --- Drop candidates (cut only) ---
            elif atype == "drop_candidates":
                cuts = [d for d in action.get("drops", [])
                        if d.get("severity") == "cut"]
                if cuts:
                    names = ", ".join(d["player"] for d in cuts[:2])
                    lines.append(f"  DROP     : {names}")

            # --- NL eligibility warnings ---
            elif atype == "nl_eligibility_warnings":
                warnings = action.get("warnings", [])
                if warnings:
                    lines.append(f"  !! NL WARNING: {warnings[0]['warning']}")

    lines.append("")
    lines.append("=" * 48)
    lines.append("  Full details below")
    lines.append("=" * 48)
    lines.append("")
    return "\n".join(lines)
