"""Run dependency-free client race/response contract tests with CI's Node."""
from pathlib import Path
import subprocess
import unittest


class RecoveryClientTests(unittest.TestCase):
    def test_node_recovery_controller_contract(self):
        path=Path(__file__).with_name('lifecycle_recovery_client.test.mjs')
        result=subprocess.run(['node','--test',str(path)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
