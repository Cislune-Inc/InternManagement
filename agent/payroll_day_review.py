"""Evidence-only day screening; never changes pay or invents break windows."""
from datetime import datetime, time, timedelta
from math import ceil
from .slack_timekeeping import timestamp


def screen(day, user, zone):
    lo = datetime.combine(datetime.fromisoformat(day['day']).date(), time(), zone)
    hi = lo + timedelta(days=1)
    lanes = []
    for source, records in [('DP', day['evidence']), ('Gusto', (day.get('gusto') or {}).get('intervals', []))]:
        for e in records:
            a, b = timestamp(e.get('start')), timestamp(e.get('end'))
            if not a or a >= hi or (b and b <= lo):
                continue
            def minute(t):
                t = max(lo, min(hi, t)).astimezone(zone)
                return 1440 if t == hi else t.hour*60+t.minute+t.second/60
            lanes.append(dict(source=source, kind=e['kind'], start=e['start'], end=e.get('end'), a=minute(a), b=minute(b) if b else minute(a), open=not bool(b)))
    # Screen one source, never count the same break twice across systems.
    source = 'DP' if day['seconds'] else 'Gusto'
    records = [e for e in lanes if e['source'] == source]
    rests = [e for e in records if e['kind'].startswith('Paid rest')]
    meals = [e for e in records if e['kind'].startswith('Lunch')]
    work = [e for e in records if e['kind'] == 'Work']
    seconds = day['seconds'] or (day.get('gusto') or {}).get('minutes', 0)*60
    h = seconds/3600
    expected = 0 if h < 3.5 else max(1, ceil((h-2)/4))
    clean_rests = {(e['a'], e['b']) for e in rests if not e['open'] and 10 <= e['b']-e['a'] <= 20 and 'pending' not in e['kind'] and any(w['a'] <= e['a'] and w['b'] >= e['b'] for w in work) and not any(m['a'] < e['b'] and m['b'] > e['a'] for m in meals)}
    messages = []
    admin = bool(user and user.worker_type == 'admin')
    if seconds and not admin:
        if len(clean_rests) < expected:
            messages.append(f'Paid breaks: {len(clean_rests)} clear / {expected} expected for recorded hours')
        if any(e['open'] or e['b']-e['a'] > 20 or e['b']-e['a'] < 10 for e in rests):
            messages.append('Break duration or return needs review')
        if h > 5:
            if not meals:
                messages.append('Lunch timing missing; check actual meal or applicable waiver')
            elif not work:
                messages.append('Lunch deadline cannot be checked without shift start')
            else:
                first = min(e['a'] for e in work)
                qualifying = [e for e in meals if not e['open'] and e['b']-e['a'] >= 30 and 'pending' not in e['kind']]
                if not qualifying or min(e['a'] for e in qualifying)-first > 300:
                    messages.append('First lunch short, late, or not verified')
        if h > 10:
            messages.append('Over 10 hours: second-meal/waiver review')
    if seconds and not work:
        messages.append('Duration only: exact start/end and break timing unavailable')
    if not seconds:
        messages.append('No recorded hours: confirm day off or missing time')
    return dict(lanes=lanes, source=source, expected_rests=None if admin else expected, recorded_rests=len(rests), clear_rests=len(clean_rests), recorded_meals=len(meals), checks=messages, label='Needs review' if day['issues'] or messages else 'Timing checks clear', basis='CA nonexempt screening only; worker classification, duty-free breaks, waivers and full source coverage still need review.')
