import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name=='nt','Linux installer')
class PrebuiltInstallTests(unittest.TestCase):
    def setUp(self):
        self.project=Path(__file__).resolve().parents[1]

    def test_bootstrap_syntax(self):
        subprocess.run(['bash','-n',str(self.project/'install.sh')],check=True)

    def test_cached_prebuilt_does_not_build_and_pins_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('start.sh','onboarding.py','runtime.py','compose.prebuilt.yaml'):
                shutil.copyfile(self.project/name,root/name)
            command_log=root/'commands'; bin=root/'bin'; bin.mkdir()
            docker=bin/'docker'
            docker.write_text('''#!/usr/bin/env python3
import sys,os,pathlib,subprocess
args=sys.argv[1:]
with open(os.environ['COMMAND_LOG'],'a') as f:f.write(' '.join(args)+'\\n')
if args[:2]==['image','inspect'] and '--format' in args:print('ghcr.io/test/guard@sha256:'+'a'*64)
if 'onboarding.py' in args:raise SystemExit(subprocess.call([sys.executable,'onboarding.py',*args[args.index('onboarding.py')+1:]]))
if 'up' in args:(pathlib.Path(os.environ['TG_GUARD_DATA_DIR'])/'.auth-login.url').write_text('http://127.0.0.1:8080/#fixture-key')
''')
            docker.chmod(0o755)
            env=dict(os.environ,PATH=str(bin)+os.pathsep+os.environ['PATH'],COMMAND_LOG=str(command_log),
                TG_GUARD_IMAGE='ghcr.io/test/guard:sha-fixture',TG_GUARD_DATA_DIR=str(root/'data'),TG_GUARD_AUTH_HOST='127.0.0.1')
            result=subprocess.run(['bash',str(root/'start.sh')],env=env,capture_output=True,text=True,timeout=15,check=True)
            self.assertIn('fixture-key',result.stdout)
            commands=command_log.read_text()
            self.assertNotIn(' build ',commands)
            self.assertIn('compose.prebuilt.yaml',commands)
            self.assertEqual((root/'.image-ref').read_text().strip(),'ghcr.io/test/guard@sha256:'+'a'*64)

    def test_bootstrap_downloads_only_three_files_and_no_app_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); bin=root/'bin';bin.mkdir(); log=root/'urls'
            curl=bin/'curl'
            curl.write_text('''#!/usr/bin/env python3
import sys,os,pathlib,json
args=sys.argv[1:];url=next(a for a in args if a.startswith('https://'))
with open(os.environ['URL_LOG'],'a') as f:f.write(url+'\\n')
output=pathlib.Path(args[args.index('-o')+1])
if '/runs?' in url:output.write_text(json.dumps({'workflow_runs':[{'head_sha':'a'*40}]}))
elif '/start.sh?' in url:output.write_text('#!/bin/bash\\necho prebuilt-ready\\n')
else:output.write_text('# fixture\\n')
''')
            curl.chmod(0o755)
            env=dict(os.environ,PATH=str(bin)+os.pathsep+os.environ['PATH'],URL_LOG=str(log),TG_GUARD_INSTALL_DIR=str(root/'installed'))
            env.pop('GH_TOKEN',None)
            result=subprocess.run(['bash',str(self.project/'install.sh')],env=env,capture_output=True,text=True,timeout=15,check=True)
            self.assertIn('prebuilt-ready',result.stdout)
            urls=log.read_text().splitlines()
            self.assertEqual(len([u for u in urls if '/contents/' in u]),3)
            self.assertFalse(list((root/'installed').rglob('*.py')))
            self.assertFalse(list((root/'installed').glob('.download.*')))

    def test_bootstrap_does_not_overwrite_unmanaged_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'keep').write_text('keep')
            result=subprocess.run(['bash',str(self.project/'install.sh')],env=dict(os.environ,TG_GUARD_INSTALL_DIR=str(root)),capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual((root/'keep').read_text(),'keep')
