"""Isolated, loopback-only owner review. NEVER mounts on the live DP service.

This executable has a local sandbox identity, not company authentication. It is
not a worker deployment path. Production must inject verified identity/grants
into create_planning_app instead. No source connection is made automatically.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from aiohttp import web
from .planning_store import PlanningStore,Principal
from .planning_web import create_planning_app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8877)
    args=parser.parse_args()
    if args.snapshot.stat().st_size>2_000_000:
        parser.error('Use a reviewed snapshot under 2 MB.')
    source=json.loads(args.snapshot.read_text())
    owner='sandbox:review-owner'
    store=PlanningStore(args.database,owner_ref=owner)
    store.initialize(source)
    principal=Principal(owner,frozenset(p['id'] for p in source['projects']))
    app=create_planning_app(store,authenticate=lambda request:principal,
                            allowed_origin=f'http://127.0.0.1:{args.port}',sandbox=True)
    web.run_app(app,host='127.0.0.1',port=args.port,access_log=None)


if __name__=='__main__':main()
