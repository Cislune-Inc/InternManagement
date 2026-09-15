"""Install only the authorized LAN preview LaunchAgent; never touch Don Pollo jobs."""
from __future__ import annotations
import argparse
import datetime
import ipaddress
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

p=argparse.ArgumentParser()
p.add_argument('--release',type=Path,required=True)
p.add_argument('--root',type=Path,required=True)
p.add_argument('--python',type=Path,required=True)
p.add_argument('--host',required=True)
p.add_argument('--network',required=True)
a=p.parse_args()
net=ipaddress.ip_network(a.network);address=ipaddress.ip_address(a.host)
if not address.is_private or address.is_unspecified or address not in net:
    raise SystemExit('Use the specific private office interface and its subnet.')
for path in (a.release/'agent/portfolio_test_server.py',a.python,a.root/'data/plan.json'):
    if not path.is_file():raise SystemExit(f'Missing required file: {path}')
for directory in (a.root,a.root/'data',a.root/'logs',a.root/'backups'):
    directory.mkdir(parents=True,exist_ok=True,mode=0o700);directory.chmod(0o700)
label='com.cislune.portfolio-preview'
plist=Path.home()/'Library/LaunchAgents'/f'{label}.plist'
plist.parent.mkdir(parents=True,exist_ok=True)
if plist.exists():
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    shutil.copy2(plist,a.root/'backups'/f'{label}-{stamp}.plist')
config={'Label':label,'ProgramArguments':[str(a.python),'-m','agent.portfolio_test_server','--host',a.host,'--port','8876','--network',a.network,'--snapshot',str(a.root/'data/plan.json'),'--feedback-db',str(a.root/'data/feedback.sqlite')],'WorkingDirectory':str(a.release),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':15,'Umask':0o077,'StandardOutPath':str(a.root/'logs/stdout.log'),'StandardErrorPath':str(a.root/'logs/stderr.log')}
plist.write_bytes(plistlib.dumps(config));plist.chmod(0o600)
domain=f'gui/{os.getuid()}'
existing=subprocess.run(['launchctl','print',f'{domain}/{label}'],capture_output=True)
if existing.returncode==0:subprocess.run(['launchctl','bootout',f'{domain}/{label}'],check=True)
subprocess.run(['launchctl','enable',f'{domain}/{label}'],check=True)
subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=True)
print(f'Installed {label} at http://{a.host}:8876/; Don Pollo jobs unchanged.')
