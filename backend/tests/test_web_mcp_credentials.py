import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from backend import web_mcp_credentials as vault
from backend import web_mcp_inspection as inspection
from backend import web_mcp_settings as settings


class CredentialTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.path=Path(temp.name)/'relay.db'
        self.plugin={'id':'reader','name':'Synthetic reader','serverId':'reader','transport':'streamable-http','enabled':True,'url':'https://example.com/mcp','options':{'bearer_token_env_var':'RELAY_SECRET'}}
        settings.save(self.path,{'revision':0,'plugins':[self.plugin]})
        self.token='synthetic-key-never-production-123456'

    def save(self,token=None,config=1,credential=0):
        return vault.write(self.path,'reader',{'revision':config,'credential_revision':credential,'bearer_token':token or self.token})

    def test_write_only_encrypted_persistent_replace_remove(self):
        self.assertFalse(vault.read(self.path,'reader')['configured'])
        value=self.save();self.assertTrue(value['configured']);self.assertFalse(value['execution_enabled']);self.assertNotIn(self.token,json.dumps(value))
        self.assertNotIn(self.token.encode(),self.path.read_bytes())
        self.assertEqual(os.stat(str(self.path)+'.mcp-key').st_mode&0o777,0o600)
        self.assertEqual(vault.load_for_inspection(self.path,self.plugin,1),(self.token,1))
        self.save('replacement-synthetic',credential=1)
        self.assertEqual(vault.load_for_inspection(self.path,self.plugin,1),('replacement-synthetic',2))
        result=vault.write(self.path,'reader',{'revision':1,'credential_revision':2},remove=True)
        self.assertFalse(result['configured']);self.assertEqual(result['credential_revision'],3)
        self.assertEqual(vault.load_for_inspection(self.path,self.plugin,1),(None,3))

    def test_conflicting_devices_and_bad_body_cannot_overwrite(self):
        self.save()
        for body in ({'revision':True,'credential_revision':1,'bearer_token':'x'},{'revision':1,'credential_revision':0,'bearer_token':'x'},{'revision':0,'credential_revision':1,'bearer_token':'x'},{'revision':1,'credential_revision':1,'bearer_token':'x','allow_tools':True}):
            with self.subTest(body=body),self.assertRaises(settings.SettingsError):vault.write(self.path,'reader',body)
        self.assertEqual(vault.load_for_inspection(self.path,self.plugin,1),(self.token,1))

    def test_invalid_header_values_are_not_saved(self):
        for token in ('','with space','newline\r\nInjected: value','中文','x'*8193):
            with self.subTest(token=token[:20]),self.assertRaises(settings.SettingsError):vault.write(self.path,'reader',{'revision':1,'credential_revision':0,'bearer_token':token})
        self.assertFalse(vault.read(self.path,'reader')['configured'])

    def test_changed_address_delete_readd_and_revert_never_restore_secret(self):
        self.save()
        other={**self.plugin,'url':'https://other.example/mcp'}
        settings.save(self.path,{'revision':1,'plugins':[other]})
        self.assertFalse(vault.read(self.path,'reader')['configured'])
        settings.save(self.path,{'revision':2,'plugins':[self.plugin]})
        self.assertFalse(vault.read(self.path,'reader')['configured'])
        self.save(config=3,credential=3)
        settings.save(self.path,{'revision':3,'plugins':[]})
        settings.save(self.path,{'revision':4,'plugins':[self.plugin]})
        self.assertFalse(vault.read(self.path,'reader')['configured'])

    def test_name_change_retains_bound_secret(self):
        self.save();settings.save(self.path,{'revision':1,'plugins':[{**self.plugin,'name':'Renamed'}]})
        self.assertTrue(vault.read(self.path,'reader')['configured'])

    def test_ciphertext_cannot_be_moved_to_another_plugin(self):
        self.save();other={**self.plugin,'id':'other','serverId':'other','url':'https://other.example/mcp'}
        settings.save(self.path,{'revision':1,'plugins':[self.plugin,other]})
        with sqlite3.connect(self.path) as conn:
            ciphertext=conn.execute('SELECT ciphertext FROM web_mcp_credentials').fetchone()[0]
            conn.execute('INSERT INTO web_mcp_credentials VALUES (?,?,?,?,?)',('other',vault.scope(other),1,ciphertext,'synthetic'))
        with self.assertRaisesRegex(settings.SettingsError,'credentials_unavailable'):vault.read(self.path,'other')

    def test_lost_or_unreadable_key_is_not_regenerated_over_existing_ciphertext(self):
        self.save();key=Path(str(self.path)+'.mcp-key');key.unlink()
        with self.assertRaises(settings.SettingsError):self.save(credential=1)
        self.assertFalse(key.exists())

    def test_concurrent_saves_share_key_and_one_revision_wins(self):
        vault.read(self.path,'reader')
        def save(_):
            try:self.save();return 'saved'
            except settings.SettingsError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(save,range(2)))
        self.assertCountEqual(results,['saved','conflict']);self.assertEqual(vault.load_for_inspection(self.path,self.plugin,1),(self.token,1))

    def test_detection_uses_bound_secret_without_reading_environment_and_redacts_echo(self):
        self.save()
        with patch.object(inspection,'discover',return_value={'tools':[{'name':'read','description':self.token}],'protocol_version':inspection.MODERN,'truncated':False}) as discover:
            result=inspection.inspect(self.path,'reader',{'revision':1})
        self.assertEqual(discover.call_args.kwargs,{'bearer_token':self.token})
        self.assertNotIn(self.token,json.dumps(result));self.assertEqual(result['tools'][0]['description'],'[redacted]')

    def test_replacing_credential_during_detection_invalidates_observation(self):
        self.save()
        def changed(*_,**kwargs):
            self.save('new-key',credential=1)
            return {'tools':[]}
        with patch.object(inspection,'discover',side_effect=changed),self.assertRaisesRegex(settings.SettingsError,'revision_conflict'):
            inspection.inspect(self.path,'reader',{'revision':1})

    def test_transport_sends_authorization_only_on_pinned_request(self):
        connection=Mock();connection.getresponse.return_value=Mock(status=401)
        with patch.object(inspection,'resolve',return_value=(inspection.urlsplit(self.plugin['url']),'8.8.8.8')),patch.object(inspection,'PinnedHTTPS',return_value=connection):
            transport=inspection.Transport(self.plugin['url'],inspection.time.monotonic()+2,bearer_token=self.token)
            with self.assertRaises(inspection.InspectionError):transport.request({'id':1,'method':'tools/list'},inspection.MODERN)
        self.assertEqual(connection.request.call_args.kwargs['headers']['Authorization'],'Bearer '+self.token)
        self.assertEqual(connection.request.call_count,1)


if __name__=='__main__':unittest.main()
