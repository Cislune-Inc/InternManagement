import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent.planning_owner_pilot import OwnerBridge,remote_program


class OwnerPilotTests(unittest.TestCase):
    def options(self):return dict(owner='OWNER',workspace='T',channel='C')
    def test_remote_program_compiles_without_writing_production(self):
        code=remote_program(self.options())
        compile(code,'remote-pilot','exec')
        self.assertNotIn('StateStore(',code.split("options={'owner'")[-1])
    def test_verified_ssh_and_stdin_transport(self):
        bridge=OwnerBridge('192.0.2.1',self.options())
        with patch('agent.planning_owner_pilot.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'owner_ref':'slack:T:OWNER'}))) as run:
            self.assertEqual(bridge.read()['owner_ref'],'slack:T:OWNER')
        self.assertIn('StrictHostKeyChecking=yes',run.call_args.args[0])
        self.assertIn('HostKeyAlias=cmacmini.local',run.call_args.args[0])
        self.assertIn('input',run.call_args.kwargs)
    def test_connection_failure_has_no_stale_result_or_secret_error(self):
        bridge=OwnerBridge('192.0.2.1',self.options())
        with patch('agent.planning_owner_pilot.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='secret',stderr='secret')):
            with self.assertRaisesRegex(ValueError,'no cached permissions') as caught:bridge.read()
        self.assertNotIn('secret',str(caught.exception))
