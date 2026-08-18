"""
Poster en automatisk gameweek-opsummering til Discord for FPL Draft-ligaen.

Kilde til data: FPL Draft's officielle API (draft.premierleague.com/api).
AI-tekst: Gemini API (kort, morsom opsummering af runden).
Destination: Discord webhook.

Køres af .github/workflows/league-update.yml på en cron. Er idempotent —
poster kun én gang pr. gameweek, styret af league-state.json i repoet.
"""
import json
import os
import sys
import urllib.request
import urllib.error

LEAGUE_ID = 668
STATE_FILE = "league-state.json"

FPL_BASE = "https://fantasy.premierleague.com/api"
DRAFT_BASE = "https://draft.premierleague.com/api"

TEAM_NAMES = {
    1: 'Arsenal', 2: 'Aston Villa', 3: 'Bournemouth', 4: 'Brentford', 5: 'Brighton',
    6: 'Chelsea', 7: 'Coventry', 8: 'Crystal Palace', 9: 'Everton', 10: 'Fulham',
    11: 'Hull', 12: 'Ipswich', 13: 'Leeds', 14: 'Liverpool', 15: 'Man City',
    16: 'Man Utd', 17: 'Newcastle', 18: "Nott'm Forest", 19: 'Spurs', 20: 'Sunderland',
}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "drafthq-bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_posted_event": 0, "last_ranks": {}, "last_transaction_count": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def find_latest_finished_event(bootstrap):
    """Finder seneste gameweek der er færdigspillet OG har låste bonuspoint."""
    candidates = [e for e in bootstrap["events"] if e["finished"] and e["data_checked"]]
    if not candidates:
        return None
    return max(candidates, key=lambda e: e["id"])


DA_WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
DA_MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
             "august", "september", "oktober", "november", "december"]


def format_deadline_da(iso_ts):
    """Formatterer en UTC-ISO-timestamp til dansk, fx 'lørdag 29. august kl. 15:00'."""
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    weekday = DA_WEEKDAYS[dt.weekday()]
    month = DA_MONTHS[dt.month - 1]
    return f"{weekday} {dt.day}. {month} kl. {dt.strftime('%H:%M')} UTC"


def find_next_deadline(bootstrap):
    upcoming = [e for e in bootstrap["events"] if not e["finished"]]
    if not upcoming:
        return None
    nxt = min(upcoming, key=lambda e: e["id"])
    return nxt["id"], nxt["deadline_time"]


def get_player_names(bootstrap):
    names = {}
    for p in bootstrap["elements"]:
        fn = (p.get("first_name") or "").strip()
        sn = (p.get("second_name") or "").strip()
        full = (fn + " " + sn).strip() or p["web_name"]
        names[p["id"]] = full
    return names


def get_entry_gw_squad(entry_id, event_id):
    """Henter en managers picks for en given gameweek. Returnerer None hvis ingen data."""
    url = f"{DRAFT_BASE}/entry/{entry_id}/event/{event_id}"
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError:
        return None
    if not isinstance(data, dict) or "picks" not in data:
        return None
    return data["picks"]


def main():
    state = load_state()

    bootstrap = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    latest_event = find_latest_finished_event(bootstrap)
    if latest_event is None:
        print("Ingen færdigspillet gameweek endnu — intet at poste.")
        return

    gw = latest_event["id"]
    if gw <= state["last_posted_event"]:
        print(f"GW{gw} er allerede postet. Intet nyt.")
        return

    player_names = get_player_names(bootstrap)

    league = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/details")

    # IMPORTANT: FPL Draft uses two different IDs per manager that do NOT always match:
    #   - league_entries[].entry_id  -> the manager's global FPL account ID (needed for
    #     per-entry endpoints like /entry/{id}/event/{gw})
    #   - league_entries[].id        -> the league-scoped membership ID (this is what
    #     standings[].league_entry and matches[] actually reference)
    # We build one name lookup keyed by BOTH ids, and a separate map from league-scoped id
    # to the real entry_id for calls that need it.
    entry_name_map = {}
    entry_id_by_league_id = {}
    for e in league["league_entries"]:
        entry_name_map[e["id"]] = e["entry_name"]
        entry_name_map[e["entry_id"]] = e["entry_name"]
        entry_id_by_league_id[e["id"]] = e["entry_id"]

    real_entry_ids = [e["entry_id"] for e in league["league_entries"]]

    # -------- live points for the gameweek --------
    live = fetch_json(f"{FPL_BASE}/event/{gw}/live")
    live_points = {int(pid): pdata["stats"]["total_points"] for pid, pdata in live["elements"].items()}

    # -------- per-entry squad + gw score (keyed by real entry_id) --------
    entry_gw_points = {}
    entry_best_player = {}   # entry_id -> (player_id, points)
    entry_worst_player = {}  # entry_id -> (player_id, points)
    league_best = None   # (points, player_id, entry_id)
    league_worst = None  # (points, player_id, entry_id)

    for entry_id in real_entry_ids:
        picks = get_entry_gw_squad(entry_id, gw)
        if not picks:
            continue
        starters = [p for p in picks if p.get("multiplier", 1) > 0]
        total = 0
        best = None
        worst = None
        for pick in starters:
            pid = pick["element"]
            pts = live_points.get(pid, 0)
            total += pts
            if best is None or pts > best[1]:
                best = (pid, pts)
            if worst is None or pts < worst[1]:
                worst = (pid, pts)
            if league_best is None or pts > league_best[0]:
                league_best = (pts, pid, entry_id)
            if league_worst is None or pts < league_worst[0]:
                league_worst = (pts, pid, entry_id)
        entry_gw_points[entry_id] = total
        if best:
            entry_best_player[entry_id] = best
        if worst:
            entry_worst_player[entry_id] = worst

    # -------- standings (cumulative, from league details) --------
    standings = sorted(league["standings"], key=lambda s: -(s["total"] or 0))
    current_ranks = {}
    standings_lines = []
    for i, s in enumerate(standings, start=1):
        eid = s["league_entry"]
        current_ranks[str(eid)] = i
        name = entry_name_map.get(eid, f"Entry {eid}")
        standings_lines.append(f"{i}. {name} — {s['total']} point")

    point_gap = None
    if len(standings) >= 2:
        point_gap = (standings[0]["total"] or 0) - (standings[-1]["total"] or 0)

    # -------- rank movement vs last posted gw --------
    movers = []
    for eid_str, rank_now in current_ranks.items():
        prev_rank = state["last_ranks"].get(eid_str)
        if prev_rank is not None:
            movers.append((entry_name_map.get(int(eid_str), eid_str), prev_rank, rank_now, prev_rank - rank_now))
    biggest_mover = max(movers, key=lambda m: abs(m[3])) if movers else None

    # -------- transactions since last post --------
    trans_data = fetch_json(f"{DRAFT_BASE}/draft/league/{LEAGUE_ID}/transactions")
    all_trans = trans_data.get("transactions", [])
    new_trans = all_trans[state["last_transaction_count"]:]
    trans_lines = []
    for t in new_trans:
        kind = t.get("kind") or t.get("result") or "transaction"
        entry_id = t.get("entry")
        player_id = t.get("element") or t.get("element_in")
        pname = player_names.get(player_id, f"spiller {player_id}")
        ename = entry_name_map.get(entry_id, f"Entry {entry_id}")
        trans_lines.append(f"{ename}: {kind} — {pname}")

    # -------- assemble context for Gemini --------
    best_line = "Ingen data"
    if league_best:
        pts, pid, eid = league_best
        best_line = f"{player_names.get(pid, pid)} ({entry_name_map.get(eid,'?')}) — {pts} point"
    worst_line = "Ingen data"
    if league_worst:
        pts, pid, eid = league_worst
        worst_line = f"{player_names.get(pid, pid)} ({entry_name_map.get(eid,'?')}) — {pts} point"

    mover_line = "Ingen ændring"
    if biggest_mover:
        name, prev_r, now_r, delta = biggest_mover
        retning = "op" if delta > 0 else "ned"
        mover_line = f"{name}: {prev_r}. → {now_r}. plads ({abs(delta)} pladser {retning})"

    context = f"""Gameweek {gw} er afsluttet i vores FPL Draft-liga "{league['league']['name']}".

STILLING:
{chr(10).join(standings_lines)}

Pointforskel mellem 1. og sidsteplads: {point_gap} point.

BEDSTE ENKELTSPILLER DENNE UGE: {best_line}
DÅRLIGSTE ENKELTSPILLER DENNE UGE (blandt startere): {worst_line}
STØRSTE PLADS-BEVÆGELSE: {mover_line}

TRANSAKTIONER SIDEN SIDST:
{chr(10).join(trans_lines) if trans_lines else "Ingen waivers eller trades siden sidst."}
"""

    summary_text = call_gemini(context)

    next_deadline = find_next_deadline(bootstrap)
    deadline_line = "Ukendt — tjek draft.premierleague.com"
    if next_deadline:
        next_gw, deadline_ts = next_deadline
        deadline_line = f"GW{next_gw}: {format_deadline_da(deadline_ts)}"

    post_to_discord(gw, standings_lines, best_line, worst_line, mover_line, trans_lines, summary_text, point_gap, deadline_line)

    # -------- persist state --------
    state["last_posted_event"] = gw
    state["last_ranks"] = current_ranks
    state["last_transaction_count"] = len(all_trans)
    save_state(state)
    print(f"GW{gw} postet og state gemt.")


def call_gemini(context):
    api_key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    prompt = (
        "Du skriver en kort, morsom opsummering (maks 120 ord, på dansk) af en runde i en lille "
        "Fantasy Premier League Draft-liga mellem venner. Brug deres holdnavne. Vær drillende og "
        "letsindig overfor dem der ligger dårligst eller performede dårligst denne uge — men hold det "
        "kammeratligt, ikke ondskabsfuldt. Skriv i løbende tekst, ingen overskrifter eller punktopstilling.\n\n"
        f"Data:\n{context}"
    )
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print("Gemini-kald fejlede, falder tilbage til simpel tekst:", e, file=sys.stderr)
        return "Ugens AI-opsummering kunne ikke genereres denne gang — men tallene taler for sig selv nedenfor."


def post_to_discord(gw, standings_lines, best_line, worst_line, mover_line, trans_lines, summary_text, point_gap, deadline_line):
    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    embed = {
        "title": f"📊 Gameweek {gw} er afgjort",
        "description": summary_text,
        "color": 2926465,
        "fields": [
            {"name": "Stilling", "value": "\n".join(standings_lines) or "—", "inline": False},
            {"name": "🔥 Ugens bedste", "value": best_line, "inline": True},
            {"name": "🥶 Ugens værste", "value": worst_line, "inline": True},
            {"name": "📈 Størst bevægelse", "value": mover_line, "inline": False},
        ],
        "footer": {"text": f"Pointgab 1.–sidsteplads: {point_gap} point"},
    }
    if trans_lines:
        embed["fields"].append({"name": "Waivers & trades siden sidst", "value": "\n".join(trans_lines), "inline": False})

    embed["fields"].append({
        "name": "⏰ Husk inden næste deadline",
        "value": f"Lav jeres trades/waivers og sæt holdet — deadline er **{deadline_line}**.",
        "inline": False,
    })

    body = json.dumps({
        "username": "Update Bot",
        "content": "@everyone",
        "embeds": [embed],
        "allowed_mentions": {"parse": ["everyone"]},
    }).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print("Discord response:", resp.status)


if __name__ == "__main__":
    main()
