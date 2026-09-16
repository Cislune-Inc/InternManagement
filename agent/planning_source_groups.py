"""Read-time provenance grouping over currently visible source revisions only."""
from datetime import datetime
import re


def _words(value):
    return re.findall(r'[a-z0-9]+', value.lower())


def _time(source):
    try:
        return datetime.fromisoformat(source['posted_at'].replace('Z', '+00:00')).timestamp()
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def group_visible_sources(sources):
    """Keep both records; never merge text, audiences, links or approvals.

    Recompute after grants and tombstones on every view, so later edits/deletes
    cannot leave a stale preferred-source hint. Exact normalized quoted text is
    required: fuzzy word overlap could silently group a contradictory update.
    """
    humans = {}
    for source in sources:
        for key in ('preferred_source', 'count_as_separate_progress', 'progress_group'):
            source.pop(key, None)
        if source.get('source_kind') == 'slack_channel' and not source.get('deleted') and source.get('meaningful'):
            humans.setdefault((source.get('scope'), source.get('person_ref'), source.get('project')), []).append(source)
    for source in sources:
        if source.get('source_kind') != 'dp_published_work_excerpt' or source.get('deleted'):
            continue
        quote = _words(' '.join(line[2:] for line in source.get('text', '').splitlines() if line.startswith('> ')))
        posted = _time(source)
        if len(set(quote)) < 5 or posted is None or not source.get('person_ref'):
            continue
        candidates = humans.get((source.get('scope'), source['person_ref'], source.get('project')), [])
        for human in sorted(candidates, key=lambda s: (-len(s.get('text', '')), s['source_ref'])):
            at = _time(human)
            words = _words(human.get('text', ''))
            if at is None or abs(at - posted) > 86400 or len(words) <= len(quote):
                continue
            if (' ' + ' '.join(quote) + ' ') not in (' ' + ' '.join(words) + ' '):
                continue
            ref = human['source_ref']
            source.update(preferred_source=ref, count_as_separate_progress=False, progress_group=ref)
            human['progress_group'] = ref
            break
    return sources
