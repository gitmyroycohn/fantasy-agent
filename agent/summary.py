"""
Phone-friendly TL;DR summary block — prepended to latest_output.md.
"""
from mlb.clock import now_et


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
    _n  = now_et()
    now = f"{_n.strftime('%a %b')} {_n.day}, {_n.year}  {_n.hour % 12 or 12}:{_n.strftime('%M')} {_n.strftime('%p')} ET"
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

        # Football leagues (agent/football_decisions.py) report format
        # "H2H Points". Before this check they matched the "H2H" test below and
        # were mislabeled as Pins & Pills with no lines under the header.
        is_football = fmt.startswith("H2H Points")

        lines.append("")
        if is_football:
            lines.append(f"[ {league_name.upper()}  |  {fmt} ]")
        elif "H2H" in fmt:
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

            # --- Football (agent/football_decisions.py action types) ---
            elif atype == "roster_legality":
                issues = action.get("issues", [])
                if action.get("legal", True) and not issues:
                    lines.append("  Roster   : legal")
                else:
                    more = f" (+{len(issues)-1} more)" if len(issues) > 1 else ""
                    first = issues[0] if issues else "roster is not legal"
                    lines.append(f"  !! Roster ILLEGAL: {first}{more}")

            elif atype == "waiver_targets":
                by_slot = action.get("by_slot", {})
                fallback = action.get("fa_source") == "fantasypros_fallback"
                top = next((e for entries in by_slot.values() for e in entries), None)
                if top:
                    pos = "/".join(top.get("positions", []))
                    lines.append(f"  Top Add  : {top['player']} ({top['team']}) [{pos}]")
                if fallback:
                    lines.append("  (free agents from FantasyPros -- CBS feed was down)")

            elif atype == "waiver_targets_unavailable":
                lines.append("  Waivers  : unavailable (CBS feed down, no fallback)")

            elif atype == "keeper_guidance":
                keeps = action.get("recommended_keeps", [])
                if action.get("is_keeper_league") and keeps:
                    lines.append(f"  Keepers  : {', '.join(keeps)}")

    lines.append("")
    lines.append("=" * 48)
    lines.append("  Full details below")
    lines.append("=" * 48)
    lines.append("")
    return "\n".join(lines)
