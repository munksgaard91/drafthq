"""
Bygger alle datafiler til Fantasy Premier League HQ (den rigtige side, ikke mockup'et):

  site-data.json       - stilling, rank-historik, GW-resuméer, sæson-highlights, alerts, bænk-tendens
  powerranking.json    - top 25 spillere, formel-baseret score + AI-argumenter
  draft-rankings.json  - top 25 pr. position (GK/DEF/MID/FWD), samme formel, til redraften i februar
  management.json      - din aktuelle startopstilling, bænk, kampprogram, ombytningsforslag

Køres af .github/workflows/site-data.yml. Data gemmes og genindlæses mellem kørsler
(rank-history.json), så vi kan bygge historik op over tid uden at have en database.

Bruger MIN_ENTRY_ID til at identificere DIG specifikt (Management-fanen er personlig,
ikke delt mellem alle i ligaen).
"""
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(__file__))
from fpl_common import (
    FPL_BASE, DRAFT_BASE, LEAGUE_ID, TEAM_NAMES,
    fetch_json, load_json_file, save_json_file,
    get_player_full_name, get_player_positions, get_player_names, get_player_clubs,
    get_league_entries, find_latest_finished_event, find_next_event,
    get_team_fixture_difficulty,
)

MY_ENTRY_ID = 1510  # Rasmus / "HaCunha Mateta" - Management-fanen er bygget til dig specifikt

RANK_HISTORY_FILE = "rank-history.json"
GW_SUMMARIES_FILE = "gw-summaries.json"


def gemini_call(prompt, expect_json=False):
    api_key = os.environ["GEMINI_API_KEY"]
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if expect_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": "Mozilla/5.0 (compatible; fplhq-bot/1.0)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def safe_gemini_json(prompt, fallback):
    try:
        text = gemini_call(prompt, expect_json=True)
        return json.loads(text)
    except Exception as e:
        print(f"Gemini JSON-kald fejlede ({e}), bruger fallback.", file=sys.stderr)
        return fallback


# ---------------------------------------------------------------------------
# Stilling, rank-historik, alerts, bænk-tendens
# ---------------------------------------------------------------------------

def build_standings_block(league_details, entry_name_map):
    standings = sorted(league_details["standings"], key=lambda s: -(s["total"] or 0))
    rows = []
    current_ranks = {}
    for i, s in enumerate(standings, start=1):
        eid = s["league_entry"]
        current_ranks[str(eid)] = i
        rows.append({
            "league_entry": eid,
            "name": entry_name_map.get(eid, f"Entry {eid}"),
            "total": s["total"] or 0,
            "rank": i,
        })
    point_gap = (rows[0]["total"] - rows[-1]["total"]) if len(rows) >= 2 else 0
    return rows, current_ranks, point_gap


def update_rank_history(rank_history, gw, current_ranks, standings_rows):
    """Gemmer stilling for denne GW i en løbende log, så vi kan tegne kurven + regne trends."""
    gw_key = f"GW{gw}"
    entry_totals = {str(r["league_entry"]): r["total"] for r in standings_rows}
    rank_history[gw_key] = {"ranks": current_ranks, "totals": entry_totals}
    return rank_history


def compute_trend(rank_history, current_ranks, this_gw):
    """Sammenligner mod forrige loggede GW. Returnerer {league_entry: delta} (+ = rykket op)."""
    prev_gw_key = f"GW{this_gw - 1}"
    prev = rank_history.get(prev_gw_key, {}).get("ranks", {})
    trend = {}
    for eid, rank in current_ranks.items():
        if eid in prev:
            trend[eid] = prev[eid] - rank  # positivt = rykket op
    return trend


def build_alerts(bootstrap, element_status, entry_name_map):
    by_id = {p["id"]: p for p in bootstrap["elements"]}
    owner_by_element = {es["element"]: es["owner"] for es in element_status if es.get("owner")}
    alerts = []
    for element_id, owner_entry_id in owner_by_element.items():
        p = by_id.get(element_id)
        if not p:
            continue
        # Kun akutte ting: helt ude, eller under 50% spilchance
        chance = p.get("chance_of_playing_next_round")
        is_acute = p["status"] in ("i", "s", "u") or (chance is not None and chance < 50)
        if not is_acute or not p.get("news"):
            continue
        alerts.append({
            "player": get_player_full_name(p),
            "club": TEAM_NAMES.get(p["team"], "?"),
            "owner_entry_id": owner_entry_id,
            "owner": entry_name_map.get(owner_entry_id, "?"),
            "news": p["news"],
            "chance": chance,
        })
    return alerts


def build_bench_trend(bootstrap, entry_ids, live_points_by_gw):
    """Kumuleret sæson-bænkpoint pr. manager, på tværs af alle spillede gameweeks."""
    totals = {str(eid): 0 for eid in entry_ids}
    for gw, live_points in live_points_by_gw.items():
        for eid in entry_ids:
            picks = get_entry_gw_picks(eid, gw)
            if not picks:
                continue
            bench = [p for p in picks if p.get("multiplier", 1) == 0]
            totals[str(eid)] += sum(live_points.get(p["element"], 0) for p in bench)
    return totals


def get_entry_gw_picks(entry_id, event_id):
    url = f"{DRAFT_BASE}/entry/{entry_id}/event/{event_id}"
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError:
        return None
    if not isinstance(data, dict) or "picks" not in data:
        return None
    return data["picks"]


# ---------------------------------------------------------------------------
# Gameweek-resumé (AI, seriøs tone, ~150 ord)
# ---------------------------------------------------------------------------

def build_gw_summary(gw, standings_rows, entry_name_map, best_line, worst_line):
    prompt = (
        "Skriv et sagligt, analytisk resumé (omkring 150 ord, på dansk) af en gameweek i en lille "
        "Fantasy Premier League Draft-liga mellem venner. Seriøs, journalistisk tone - IKKE drillende "
        "eller morsom, det er en anden kanal end vores Discord-bot. Brug holdenes navne. Del op i "
        "korte afsnit. Nævn kort hvem der lå bedst og dårligst, og en generel observation om ugen.\n\n"
        f"Stilling efter GW{gw}:\n"
        + "\n".join(f"{r['rank']}. {r['name']} — {r['total']} point" for r in standings_rows)
        + f"\n\nBedste enkeltspiller: {best_line}\nDårligste enkeltspiller: {worst_line}\n"
    )
    try:
        return gemini_call(prompt)
    except Exception as e:
        print(f"GW-resumé fejlede ({e}), springer over.", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Powerranking / Draft-rankings (formel + AI-argumenter)
# ---------------------------------------------------------------------------

def compute_power_score(p, fixture_by_team, season_started):
    """
    Vægtet Power Score:
      55% form (FPL's egen 'form'-stat; falder tilbage til points_per_game før sæsonstart)
      25% fixture-sværhedsgrad (næste kamp, omvendt skala - let fixture = højere score)
      15% nylig trend (form vs. sæsongennemsnit - kun meningsfuldt når sæsonen er i gang)
      5%  nettotransfers ind denne uge (momentum)
    Returnerer et 0-100-agtigt tal, ikke en eksakt procent.
    """
    form = float(p.get("form") or 0)
    ppg = float(p.get("points_per_game") or 0)
    effective_form = form if form > 0 else ppg  # fallback før sæsonstart

    fixtures = fixture_by_team.get(p["team"], [])
    next_diff = fixtures[0] if fixtures else 3
    fixture_score = (5 - next_diff) / 4 * 10  # 1=let->10, 5=svært->0

    if season_started and form > 0:
        trend = form - ppg  # er formen bedre end sæson-snittet lige nu?
    else:
        trend = 0

    net_transfers = (p.get("transfers_in_event") or 0) - (p.get("transfers_out_event") or 0)
    transfer_score = max(-5, min(5, net_transfers / 20000))  # dæmpet, undgår at ét viralt navn dominerer

    raw = (effective_form * 0.55) + (fixture_score * 0.25) + (trend * 0.15) + (transfer_score * 0.05)
    return round(raw * 10, 1)  # skaleret til en mere "point-agtig" 0-100ish størrelse


def build_ranked_list(bootstrap, fixture_by_team, season_started, position_filter=None, top_n=25):
    positions = get_player_positions(bootstrap)
    scored = []
    for p in bootstrap["elements"]:
        if p.get("removed"):
            continue
        if p["status"] not in ("a", "d"):  # udelad langtidsskadede/suspenderede helt fra ranking
            continue
        pos = positions[p["id"]]
        if position_filter and pos != position_filter:
            continue
        minutes = float(p.get("minutes") or 0)
        if minutes == 0 and float(p.get("total_points") or 0) == 0:
            continue  # ingen reelt spillegrundlag at vurdere ud fra
        # Reliability-filter: før sæsonstart bruger vi sidste sæsons points_per_game som
        # form-proxy - men et gennemsnit fra få kampe er ikke retvisende (fx en reserve-
        # målmand med 1 kamp og et enkelt clean sheet). Kræv et minimum af spilletid for
        # at undgå at småsample-outliers topper listen.
        if not season_started and minutes < 900:
            continue
        score = compute_power_score(p, fixture_by_team, season_started)
        scored.append({
            "id": p["id"],
            "name": get_player_full_name(p),
            "club": TEAM_NAMES.get(p["team"], "?"),
            "team_id": p["team"],
            "pos": pos,
            "score": score,
            "total_points": p.get("total_points", 0),
            "form": p.get("form"),
            "status": p["status"],
            "chance": p.get("chance_of_playing_next_round"),
        })
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_n]


def add_ai_arguments(ranked_list, list_label, fixture_by_team=None):
    """Ét samlet Gemini-kald pr. liste (ikke ét pr. spiller) - langt billigere og hurtigere."""
    if not ranked_list:
        return ranked_list
    lines = []
    for i, p in enumerate(ranked_list):
        fixt = ""
        if fixture_by_team:
            diffs = fixture_by_team.get(p.get("team_id"), [])
            if diffs:
                avg = sum(diffs[:3]) / len(diffs[:3])
                fixt = f", næste 3 kampes sværhedsgrad-snit {avg:.1f}/5"
        lines.append(
            f"{i+1}. {p['name']} ({p['club']}, {p['pos']}) - {p['total_points']} point sidste sæson, "
            f"status {p['status']}{fixt}"
        )
    players_block = "\n".join(lines)
    prompt = (
        f"Du får en rangeret liste over {list_label} i Fantasy Premier League. Skriv ÉT kort, "
        "naturligt argument pr. spiller (maks 20 ord, på dansk) for hvorfor de er et godt/dårligt "
        "valg lige nu. Varier formuleringen mellem spillerne - gentag IKKE samme sætningsskabelon "
        "('med en score på X og Y point...') for hver spiller. Brug KUN de tal der er givet, opfind "
        "aldrig kampresultater, mål eller hændelser der ikke fremgår af dataen - er der ikke andet at "
        "sige end pointtal og fixtures, så sig det naturligt uden at lyde robotagtigt.\n\n"
        f"{players_block}\n\n"
        'Svar KUN som gyldig JSON: en liste af strenge, i samme rækkefølge som spillerne, '
        'fx ["argument 1", "argument 2", ...]. Ingen anden tekst.'
    )
    fallback = [f"{p['total_points']} point sidste sæson." for p in ranked_list]
    args = safe_gemini_json(prompt, fallback)
    if not isinstance(args, list) or len(args) != len(ranked_list):
        args = fallback
    for p, arg in zip(ranked_list, args):
        p["argument"] = arg
    return ranked_list


# ---------------------------------------------------------------------------
# Management (kun for MY_ENTRY_ID): startopstilling, bænk, ombytninger
# ---------------------------------------------------------------------------

def build_management(bootstrap, current_gw, fixture_by_team):
    positions = get_player_positions(bootstrap)
    names = get_player_names(bootstrap)
    clubs = get_player_clubs(bootstrap)
    by_id = {p["id"]: p for p in bootstrap["elements"]}

    picks = get_entry_gw_picks(MY_ENTRY_ID, current_gw) if current_gw else None
    if not picks:
        return {"available": False, "reason": "Ingen picks-data for denne gameweek endnu."}

    starters, bench = [], []
    squad_team_ids = set()
    injury_news = []
    for pick in picks:
        pid = pick["element"]
        p = by_id.get(pid)
        if not p:
            continue
        squad_team_ids.add(p["team"])
        if p["status"] != "a" and p.get("news"):
            injury_news.append({
                "player": names.get(pid, "?"), "club": clubs.get(pid, "?"),
                "news": p["news"], "status": p["status"],
                "chance": p.get("chance_of_playing_next_round"),
            })
        entry = {
            "id": pid, "name": names.get(pid, "?"), "club": clubs.get(pid, "?"),
            "pos": positions.get(pid, "?"), "status": p["status"],
            "chance": p.get("chance_of_playing_next_round"),
        }
        (starters if pick.get("multiplier", 1) > 0 else bench).append(entry)

    # Ombytningsforslag: kun starter->bænk-spillere på SAMME position, og kun hvis starteren
    # er flagget (skadet/tvivlsom). Maks 3, ingen hvis der ikke er noget reelt problem.
    suggestions = []
    for s in starters:
        if s["status"] == "a" and (s["chance"] is None or s["chance"] >= 75):
            continue  # ingen problem med denne starter
        same_pos_bench = [b for b in bench if b["pos"] == s["pos"] and b["status"] == "a"]
        if same_pos_bench:
            best_alt = same_pos_bench[0]
            suggestions.append({
                "out": s["name"], "in": best_alt["name"], "type": "Line-up",
                "reason": f"{s['name']} er flagget ({s['chance']}% spilchance) - {best_alt['name']} er tilgængelig på samme position.",
            })
        else:
            suggestions.append({
                "out": s["name"], "in": None, "type": "Formation",
                "reason": f"{s['name']} er flagget ({s['chance']}% spilchance), men ingen bænket spiller på samme position kan erstatte 1:1 - overvej et formationsskift.",
            })
    suggestions = suggestions[:3]

    # Kampprogram for de klubber der reelt er repræsenteret i truppen
    club_counts = {}
    for entry in starters + bench:
        club_counts[entry["club"]] = club_counts.get(entry["club"], 0) + 1
    fixtures_block = []
    for team_id in squad_team_ids:
        club_name = TEAM_NAMES.get(team_id, "?")
        fixtures_block.append({
            "club": club_name,
            "count": club_counts.get(club_name, 0),
            "difficulty": (fixture_by_team or {}).get(team_id, []),
        })
    fixtures_block.sort(key=lambda x: x["club"])

    return {
        "available": True,
        "starters": starters,
        "bench": bench,
        "suggestions": suggestions,
        "fixtures": fixtures_block,
        "injury_news": injury_news,
    }


def build_transaction_history(league_details_id, entry_name_map, player_names):
    data = fetch_json(f"{DRAFT_BASE}/draft/league/{league_details_id}/transactions")
    kind_labels = {"w": "Waiver", "f": "Free agent", "t": "Trade"}
    out = []
    for t in data.get("transactions", []):
        if t.get("result") != "a":  # kun gennemførte (accepterede) transaktioner
            continue
        kind = kind_labels.get(t.get("kind"), t.get("kind", "Transaktion"))
        entry_id = t.get("entry")
        in_name = player_names.get(t.get("element_in"))
        out_name = player_names.get(t.get("element_out"))
        out.append({
            "gw": t.get("event"),
            "entry_name": entry_name_map.get(entry_id, f"Entry {entry_id}"),
            "kind": kind,
            "player_in": in_name,
            "player_out": out_name,
            "added": t.get("added"),
        })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    bootstrap = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    fixtures = fetch_json(f"{FPL_BASE}/fixtures/")
    league_details = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/details")
    element_status = fetch_json(f"{DRAFT_BASE}/league/{LEAGUE_ID}/element-status")["element_status"]

    entry_name_map, entry_id_by_league_id, real_entry_ids = get_league_entries(league_details)

    latest_event = find_latest_finished_event(bootstrap)
    next_event = find_next_event(bootstrap)
    current_gw = latest_event["id"] if latest_event else 0
    season_started = latest_event is not None

    fixture_by_team = get_team_fixture_difficulty(fixtures, num_gws=5)

    # ---- standings + rank history ----
    standings_rows, current_ranks, point_gap = build_standings_block(league_details, entry_name_map)
    rank_history = load_json_file(RANK_HISTORY_FILE, {})
    if season_started:
        rank_history = update_rank_history(rank_history, current_gw, current_ranks, standings_rows)
        save_json_file(RANK_HISTORY_FILE, rank_history)
    trend = compute_trend(rank_history, current_ranks, current_gw) if season_started else {}
    for r in standings_rows:
        r["trend"] = trend.get(str(r["league_entry"]))

    # ---- alerts ----
    alerts = build_alerts(bootstrap, element_status, entry_name_map)

    # ---- gw summaries (kun ved en NY færdigspillet gameweek) ----
    gw_summaries = load_json_file(GW_SUMMARIES_FILE, [])
    already_summarized = {s["gw"] for s in gw_summaries}
    if season_started and current_gw not in already_summarized:
        best_line = worst_line = "Ingen data"  # kræver picks pr. manager - se league_update.py for fuld logik
        text = build_gw_summary(current_gw, standings_rows, entry_name_map, best_line, worst_line)
        if text:
            gw_summaries.insert(0, {"gw": current_gw, "text": text})
            gw_summaries = gw_summaries[:10]  # behold kun de seneste 10
            save_json_file(GW_SUMMARIES_FILE, gw_summaries)

    # ---- season highlights ----
    highlights = {"highest_gw_score": None, "longest_streak": None}
    if len(rank_history) >= 1:
        sorted_gws = sorted(rank_history.keys(), key=lambda k: int(k[2:]))
        best_score, best_entry, best_gw = 0, None, None
        prev_totals = {}
        for gw_key in sorted_gws:
            totals = rank_history[gw_key]["totals"]
            for eid, total in totals.items():
                gw_score = total - prev_totals.get(eid, 0)  # denne uges point, ikke kumuleret total
                if gw_score > best_score:
                    best_score, best_entry, best_gw = gw_score, eid, gw_key
            prev_totals = totals
        if best_entry:
            highlights["highest_gw_score"] = {
                "name": entry_name_map.get(int(best_entry), "?"), "score": best_score, "gw": best_gw
            }

    # ---- bench trend (kun hvis sæsonen er i gang - ellers dyrt/meningsløst at hente) ----
    bench_trend = {}
    if season_started:
        live_points_by_gw = {}
        for gw in range(1, current_gw + 1):
            live = fetch_json(f"{FPL_BASE}/event/{gw}/live")
            live_points_by_gw[gw] = {int(pid): d["stats"]["total_points"] for pid, d in live["elements"].items()}
        bench_trend = build_bench_trend(bootstrap, real_entry_ids, live_points_by_gw)

    # ---- transaktionshistorik ----
    player_names_all = get_player_names(bootstrap)
    transactions = build_transaction_history(LEAGUE_ID, entry_name_map, player_names_all)

    site_data = {
        "updated": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "current_gw": current_gw,
        "season_started": season_started,
        "next_deadline": next_event["deadline_time"] if next_event else None,
        "standings": standings_rows,
        "point_gap": point_gap,
        "alerts": alerts,
        "gw_summaries": gw_summaries,
        "highlights": highlights,
        "bench_trend": bench_trend,
        "transactions": transactions,
    }
    save_json_file("site-data.json", site_data)
    print(f"site-data.json skrevet ({len(standings_rows)} hold, GW{current_gw}, season_started={season_started})")

    # ---- powerranking ----
    pr_list = build_ranked_list(bootstrap, fixture_by_team, season_started, position_filter=None, top_n=25)
    pr_list = add_ai_arguments(pr_list, "spillere i Fantasy Premier League (alle positioner)", fixture_by_team)
    save_json_file("powerranking.json", {"updated": site_data["updated"], "players": pr_list})
    print(f"powerranking.json skrevet ({len(pr_list)} spillere)")

    # ---- draft rankings (pr. position) ----
    draft_data = {"updated": site_data["updated"], "positions": {}}
    for pos in ("GK", "DEF", "MID", "FWD"):
        lst = build_ranked_list(bootstrap, fixture_by_team, season_started, position_filter=pos, top_n=25)
        lst = add_ai_arguments(lst, f"{pos}-spillere i Fantasy Premier League", fixture_by_team)
        draft_data["positions"][pos] = lst
        print(f"  draft/{pos}: {len(lst)} spillere")
    save_json_file("draft-rankings.json", draft_data)
    print("draft-rankings.json skrevet")

    # ---- management (kun dig) ----
    management = build_management(bootstrap, current_gw if season_started else next_event["id"] if next_event else None, fixture_by_team)
    management["updated"] = site_data["updated"]
    save_json_file("management.json", management)
    print("management.json skrevet, available=", management.get("available"))


if __name__ == "__main__":
    main()
