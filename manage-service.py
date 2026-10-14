#!/usr/bin/env python3
"""Install/start or stop the macOS login service for this library."""
import argparse
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

label='io.jdjuan.skills-library'
base=Path(__file__).resolve().parent
plist=Path.home()/'Library/LaunchAgents'/f'{label}.plist'
domain=f'gui/{os.getuid()}'
parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['start','stop','status'])
args=parser.parse_args()
if args.action=='start':
    logs=Path.home()/'Library/Logs/SkillsLibrary'
    logs.mkdir(parents=True,exist_ok=True)
    plist.parent.mkdir(parents=True,exist_ok=True)
    config={'Label':label,'ProgramArguments':[shutil.which('python3'),str(base/'server.py')], 'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'WorkingDirectory':str(base),'StandardOutPath':str(logs/'server.log'),'StandardErrorPath':str(logs/'error.log'),'EnvironmentVariables':{'PYTHONUNBUFFERED':'1'}}
    subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    plist.write_bytes(plistlib.dumps(config))
    subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=True)
    print('Installed login service: http://127.0.0.1:8765')
elif args.action=='stop':
    subprocess.run(['launchctl','bootout',domain+'/'+label],capture_output=True)
    plist.unlink(missing_ok=True)
    print('Stopped and removed login service.')
else:
    subprocess.run(['launchctl','print',domain+'/'+label],check=True)
