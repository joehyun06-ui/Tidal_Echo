"""Real browser DOM tests; synthetic transport, loopback-only static modules."""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import unittest
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]

class RecoveryPanelBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files={'/':b'<!doctype html><html lang="zh"><meta charset="utf-8"><title>Recovery test</title><div id="root"></div></html>'}
        for name in ('lifecycle_recovery_client.mjs','lifecycle_recovery_panel.mjs'):
            files['/'+name]=(ROOT/'examples'/name).read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                value=files.get(self.path)
                self.send_response(200 if value is not None else 404)
                self.send_header('Content-Type','text/javascript; charset=utf-8' if self.path.endswith('.mjs') else 'text/html; charset=utf-8')
                self.end_headers();self.wfile.write(value or b'')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin='http://127.0.0.1:'+str(cls.server.server_port)
        cls.playwright=sync_playwright().start()
        cls.browser=getattr(cls.playwright,os.environ.get('RECOVERY_BROWSER','chromium')).launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join(5)

    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':390,'height':844});self.addCleanup(self.context.close)
        self.external=[]
        def route(request):
            if request.request.url.startswith(self.origin+'/'):request.continue_()
            else:self.external.append(request.request.url);request.abort()
        self.context.route('**/*',route)
        self.page=self.context.new_page();self.errors=[];self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.goto(self.origin)
        self.page.evaluate('''async()=>{
          window.mount=(await import('/lifecycle_recovery_panel.mjs')).mountRecoveryPanel;
          window.ref={conversation_id:'synthetic_chat',activation_request_id:'activate_1',attempt_request_id:'attempt_1',packet_sha256:'a'.repeat(64)};
          window.reply=(state='historical_confirmed')=>({status:200,body:{ok:true,state,historical:state==='historical_confirmed',revision_applied:state==='historical_confirmed'?3:null,window_confirmed_at_apply:state==='historical_confirmed',current_window_confirmed:false,reply_completed:false,retry_authorized:false}});
          window.calls=0;
        }''')

    def tearDown(self):
        self.assertEqual(self.errors,[]);self.assertEqual(self.external,[])

    def test_default_off_manual_check_and_historical_wording(self):
        self.page.evaluate("mount(document.querySelector('#root'),{observe:()=>{calls++;}})")
        self.assertEqual(self.page.locator('section').count(),0)
        self.page.evaluate("window.panel=mount(document.querySelector('#root'),{enabled:true,observe:async()=>{calls++;return reply();}});panel.bind('owner',ref)")
        self.assertEqual(self.page.evaluate('calls'),0)
        self.page.get_by_role('button',name='核对原请求').click()
        self.page.get_by_role('status').filter(has_text='原请求曾完成激活确认。').wait_for()
        self.assertEqual(self.page.evaluate('calls'),1)
        self.assertIn('当前窗口可用性仍需单独确认',self.page.locator('section').inner_text())
        self.assertEqual(self.page.locator('button').count(),1)
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))
        self.page.evaluate('panel.dispose()');self.assertEqual(self.page.locator('section').count(),0)

    def test_pending_rebind_dispose_and_late_result_do_not_restore_old_dom(self):
        self.page.evaluate("window.panel=mount(document.querySelector('#root'),{enabled:true,observe:(_,o)=>{calls++;window.signal=o.signal;return new Promise(r=>window.finish=r);}});panel.bind('old',ref)")
        button=self.page.get_by_role('button',name='核对原请求');button.click();self.assertTrue(button.is_disabled())
        self.assertEqual(self.page.locator('section').get_attribute('aria-busy'),'true')
        self.page.evaluate("panel.bind('new',{...ref,conversation_id:'other_chat'});finish(reply())")
        self.page.get_by_role('status').filter(has_text='尚未核对。').wait_for()
        self.assertTrue(self.page.evaluate('signal.aborted'));self.assertEqual(self.page.evaluate('calls'),1)
        button.click();self.page.evaluate('panel.dispose();finish(reply())')
        self.assertEqual(self.page.locator('section').count(),0)

    def test_failure_retains_original_and_revocation_withdraws_controls(self):
        self.page.evaluate("window.panel=mount(document.querySelector('#root'),{enabled:true,observe:async r=>{window.last=r;calls++;if(calls===1)throw Error('<img src=x onerror=alert(1)>');return {status:403};}});panel.bind('owner',ref)")
        button=self.page.get_by_role('button',name='核对原请求');button.click()
        self.page.get_by_role('status').filter(has_text='原请求已保留').wait_for()
        self.assertEqual(self.page.evaluate('JSON.stringify(last)===JSON.stringify(ref)'),True)
        self.assertEqual(self.page.locator('img').count(),0);self.assertFalse(button.is_disabled())
        button.click();self.page.get_by_role('status').filter(has_text='身份或权限已变化').wait_for()
        self.assertTrue(button.is_disabled());self.assertEqual(self.page.evaluate('calls'),2)

if __name__=='__main__':unittest.main()
