import copy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from backend import web_mcp_settings as settings


class McpSettingsTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.path=Path(temp.name)/'relay.db'
        self.plugin={'id':'plugin-1','name':'synthetic reader','serverId':'reader','transport':'streamable-http',
                     'enabled':True,'url':'https://example.com/mcp','options':{'bearer_token_env_var':'READER_KEY','enabled_tools':['read']}}
    def save(self,revision=0,plugins=None):return settings.save(self.path,{'revision':revision,'plugins':plugins if plugins is not None else [self.plugin]})
    def test_persist_edit_delete_and_execution_remains_disabled(self):
        self.assertEqual(settings.read(self.path)['revision'],0)
        saved=self.save();self.assertEqual(settings.read(self.path),saved)
        self.assertFalse(saved['execution_enabled']);self.assertEqual(saved['connection_status'],'not_connected')
        self.plugin['enabled']=False;self.save(1);self.assertFalse(settings.read(self.path)['plugins'][0]['enabled'])
        self.save(2,[]);self.assertEqual(settings.read(self.path)['plugins'],[])
    def test_stale_device_cannot_overwrite_or_delete(self):
        self.save()
        with self.assertRaisesRegex(settings.SettingsError,'revision_conflict'):self.save(0,[])
        self.assertEqual(settings.read(self.path)['revision'],1)
    def test_concurrent_devices_have_one_winner(self):
        settings.read(self.path)
        def save(_):
            try:self.save();return 'saved'
            except settings.SettingsError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(save,range(2)))
        self.assertCountEqual(results,['saved','conflict']);self.assertEqual(settings.read(self.path)['revision'],1)
    def test_rejects_credential_values_and_unknown_fields(self):
        for options in ({'env':{'KEY':'secret'}},{'http_headers':{'Authorization':'secret'}},{'bearer_token_env_var':'a secret'},{'env_http_headers':{'Authorization':'not a variable'}},{'required':1},{'tool_timeout_sec':True},{'tool_timeout_sec':float('nan')}):
            with self.subTest(options=options),self.assertRaises(settings.SettingsError):settings.validate_plugin({**self.plugin,'options':options})
        for url in ['https://a:b@example.com/mcp','https://example.com/mcp?token=secret','https://example.com/#secret','file:///tmp/a','https://','https://example.com:bad/mcp']:
            with self.subTest(url=url),self.assertRaises(settings.SettingsError):settings.validate_plugin({**self.plugin,'url':url})
        self.assertEqual(settings.read(self.path)['revision'],0)
    def test_bounds_ids_and_duplicates(self):
        for plugins in ([self.plugin]*2,[{**self.plugin,'id':'other'},self.plugin],[self.plugin]*51):
            with self.assertRaises(settings.SettingsError):self.save(plugins=plugins)
        for body in ({'revision':True,'plugins':[]},{'revision':-1,'plugins':[]},{'revision':0,'plugins':[],'extra':True}):
            with self.assertRaises(settings.SettingsError):settings.save(self.path,body)
        with self.assertRaises(settings.SettingsError):settings.validate_plugin({**self.plugin,'id':'../a'})
    def test_stdio_is_only_stored_and_never_interpreted(self):
        plugin={k:v for k,v in self.plugin.items() if k!='url'}
        plugin.update(transport='stdio',command='never-run-this-command',args=['$(never-run)'],options={'env_vars':['READER_KEY'],'cwd':'/synthetic','disabled_tools':['write']})
        self.save(plugins=[plugin]);self.assertEqual(settings.read(self.path)['plugins'],[plugin])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('web_mcp_settings',)])


if __name__=='__main__':unittest.main()
