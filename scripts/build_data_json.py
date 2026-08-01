"""
Henter FPL's officielle bootstrap-static API og bygger data.json,
som index.html loader ved runtime (samme repo => ingen CORS-problem).

Køres af .github/workflows/update-data.yml på en cron.
Forventer at bootstrap.json allerede er hentet til samme mappe
(se workflow-filen), for at holde denne fil simpel og afhængighedsfri.
"""
import json
from datetime import datetime, timezone

TEAM_NAMES = {
    1: 'Arsenal', 2: 'Aston Villa', 3: 'Bournemouth', 4: 'Brentford', 5: 'Brighton',
    6: 'Chelsea', 7: 'Coventry', 8: 'Crystal Palace', 9: 'Everton', 10: 'Fulham',
    11: 'Hull', 12: 'Ipswich', 13: 'Leeds', 14: 'Liverpool', 15: 'Man City',
    16: 'Man Utd', 17: 'Newcastle', 18: "Nott'm Forest", 19: 'Spurs', 20: 'Sunderland',
}
POS_MAP = {'GKP': 'GK', 'DEF': 'DEF', 'MID': 'MID', 'FWD': 'FWD'}


def full_name(p):
    fn = (p.get('first_name') or '').strip()
    sn = (p.get('second_name') or '').strip()
    name = (fn + ' ' + sn).strip()
    return name if name else p['web_name']


def main():
    with open('bootstrap.json', encoding='utf-8') as f:
        d = json.load(f)

    teams = {t['id']: TEAM_NAMES.get(t['id'], t['short_name']) for t in d['teams']}
    pos = {p['id']: POS_MAP[p['singular_name_short']] for p in d['element_types']}

    players = []
    for p in d['elements']:
        if p.get('removed'):
            continue
        players.append({
            'id': p['id'],
            'name': full_name(p),
            'club': teams[p['team']],
            'pos': pos[p['element_type']],
            'pts': p['total_points'],
            'status': p['status'],
        })

    out = {
        'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'players': players,
    }

    with open('data.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))

    print(f"Wrote {len(players)} players to data.json")


if __name__ == '__main__':
    main()
