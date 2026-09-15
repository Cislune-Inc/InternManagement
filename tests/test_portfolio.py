import json
from pathlib import Path
import pytest
from agent.portfolio import load_portfolio, render_portfolio

def test_empty_source_does_not_create_storage(tmp_path):
    assert load_portfolio(tmp_path)['tasks'] == []
    assert not (tmp_path/'portfolio').exists()

def test_source_read_only_and_script_safe(tmp_path):
    p=tmp_path/'portfolio'/'plan.json';p.parent.mkdir()
    plan={'schema_version':1,'revision':'test','projects':[], 'tasks':[], 'notice':'</script><script>alert(1)</script>'}
    p.write_text(json.dumps(plan));original=p.read_bytes()
    page=render_portfolio(load_portfolio(tmp_path))
    assert p.read_bytes()==original
    assert '</script><script>alert(1)' not in page
    assert '\\u003c/script\\u003e' in page
    assert '__ENGINE__' not in page and '__APP__' not in page
    assert 'See the path' in page

def test_bad_source_schema(tmp_path):
    p=tmp_path/'portfolio'/'plan.json';p.parent.mkdir();p.write_text('{}')
    with pytest.raises(ValueError):load_portfolio(tmp_path)
