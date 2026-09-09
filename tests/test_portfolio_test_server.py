import json,re,sqlite3,threading
from http.server import ThreadingHTTPServer
import requests
from agent.portfolio_test_server import FeedbackStore, make_handler, team_snapshot

def test_projection_drops_owner_only_extra_fields():
    plan={'schema_version':1,'revision':'test','as_of':'today','scenario_start':'2026-09-09','private_dm':'secret','projects':[{'id':'x','name':'X','salary':'secret'}],'tasks':[{'id':'t','project':'x','private_notes':'secret'}]}
    result=team_snapshot(plan)
    assert 'secret' not in json.dumps(result)

def test_preview_boundary_feedback_and_retry(tmp_path):
    plan={'schema_version':1,'revision':'test','as_of':'today','scenario_start':'2026-09-09','projects':[],'tasks':[]}
    store=FeedbackStore(tmp_path/'feedback.sqlite')
    server=ThreadingHTTPServer(('127.0.0.1',0),lambda *_:None)
    port=server.server_port
    server.RequestHandlerClass=make_handler(plan,store,host='127.0.0.1',port=port,network='127.0.0.0/8')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{port}'
    try:
        page=requests.get(base,timeout=2)
        assert page.status_code==200 and 'On-site team test' in page.text
        assert 'default-src' in page.headers['Content-Security-Policy']
        token=re.search(r"'X-Preview-CSRF':\"([^\"]+)\"",page.text).group(1)
        for path in ['/payroll','/work','/api/portfolio-data','/feedback.sqlite','/../../secrets']:
            assert requests.get(base+path,timeout=2).status_code==404
        assert requests.get(base,headers={'Host':'evil.example'},timeout=2).status_code==403
        payload={'id':'fixture-id','name':'Test fixture','project':'test','message':'Test only'}
        assert requests.post(base+'/api/feedback',json=payload,timeout=2).status_code==403
        headers={'Origin':base,'X-Preview-CSRF':token}
        assert requests.post(base+'/api/feedback',json=payload,headers={**headers,'Origin':'http://evil.example'},timeout=2).status_code==403
        for _ in range(2):assert requests.post(base+'/api/feedback',json=payload,headers=headers,timeout=2).status_code==200
        with sqlite3.connect(store.path) as db:assert db.execute('SELECT count(*) FROM feedback').fetchone()[0]==1
        assert requests.post(base+'/api/feedback',json={**payload,'message':'x'*4001},headers=headers,timeout=2).status_code==400
    finally:server.shutdown();thread.join();server.server_close()
