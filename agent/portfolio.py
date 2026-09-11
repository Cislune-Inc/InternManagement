"""Owner planning view: read-only source snapshot, browser-local scenarios only."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

ASSETS = Path(__file__).with_name('portfolio_assets')

def load_portfolio(storage_root: Path) -> dict:
    path = storage_root / 'portfolio' / 'plan.json'
    if not path.is_file():
        return {'schema_version': 1, 'revision': 'empty', 'as_of': '', 'projects': [], 'tasks': [],
                'notice': 'No reviewed portfolio snapshot loaded. Import a planning scenario to begin.'}
    if path.stat().st_size > 2_000_000:
        raise ValueError('Portfolio snapshot exceeds 2 MB.')
    plan = json.loads(path.read_text())
    if not isinstance(plan, dict) or plan.get('schema_version') != 1:
        raise ValueError('Portfolio snapshot requires schema version 1.')
    return plan

def render_portfolio(plan: dict) -> str:
    data = json.dumps(plan, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return (ASSETS / 'planner.html').read_text().replace('__ENGINE__', (ASSETS / 'schedule.js').read_text()).replace('__APP__', (ASSETS / 'app.js').read_text()).replace('__PLAN__', data)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Render a private local portfolio preview without starting Don Pollo.')
    parser.add_argument('--storage-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_portfolio(load_portfolio(args.storage_root)))
    args.output.chmod(0o600)
