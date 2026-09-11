from datetime import datetime, timezone

from agent.state_store import StateStore
from agent.slack_work_intake import SlackWorkIntake
from agent.work_evidence import source_references


def test_links_are_canonical_unverified_and_not_credentials():
    refs = source_references('Result <https://github.com/Cislune-Inc/demo/pull/3?token=secret|PR> '
                             'https://drive.google.com/file/d/abc/view?usp=sharing '
                             'https://github.com.evil.test/repo '
                             'https://user:secret@github.com/repo '
                             'https://cad.onshape.com/documents/d/v/v/e/e')
    assert [r['source'] for r in refs] == ['github', 'drive', 'onshape']
    assert all(r['retrieval_status'] == 'not_verified' for r in refs)
    assert 'secret' not in str(refs)


def test_handoff_is_owned_deduplicated_and_corrections_preserve_original(tmp_path):
    store = StateStore(tmp_path / 'state.sqlite3')
    intake = SlackWorkIntake(store)
    def send(text, event, actor='W'):
        return intake.handle(actor_id=actor, actor_name=actor, is_manager=False,
                             text=text, event_id=event, now=datetime.now(timezone.utc))
    send('work IRAD: compare material coupons and save plots https://drive.google.com/file/d/abc/view', '1')
    send('work update same source https://drive.google.com/file/d/abc/view', '2')
    handoff = send('work handoff', '3')
    assert 'access/content not verified' in handoff
    assert 'abc' not in send('work evidence', '4', actor='OTHER')
    send('work edit Comparison is still blocked by missing calibration', '5')
    with store._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM work_evidence').fetchone()[0] == 1
        kinds = [r[0] for r in conn.execute('SELECT kind FROM work_intake_events')]
        assert kinds == ['proposal', 'update', 'edit']
        assert conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_bounded_links_and_no_network():
    refs = source_references(' '.join(f'https://github.com/org/repo/pull/{i}' for i in range(30)))
    assert len(refs) == 5
