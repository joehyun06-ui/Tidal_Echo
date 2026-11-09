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
        for name in ('lifecycle_recovery_client.mjs','lifecycle_recovery_panel.mjs',
                     'lifecycle_recovery_reference_store.mjs','lifecycle_recovery_restore.mjs'):
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

    def test_reference_survives_reload_without_automatic_request(self):
        value=self.page.evaluate('''async()=>{
          const {createRecoveryReferenceStore}=await import('/lifecycle_recovery_reference_store.mjs');
          window.store=createRecoveryReferenceStore({enabled:true,indexedDB});
          await store.save('owner',ref);return ref;
        }''')
        self.page.reload()
        restored=self.page.evaluate('''async()=>{
          const {createRecoveryReferenceStore}=await import('/lifecycle_recovery_reference_store.mjs');
          window.store=createRecoveryReferenceStore({enabled:true,indexedDB});
          window.saved=await store.load('owner','synthetic_chat');return saved;
        }''')
        self.assertEqual(restored,value)
        self.page.evaluate('''async()=>{
          window.calls=0;const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          window.restore=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
            loadReference:(owner,chat)=>store.load(owner,chat),observe:async()=>{calls++;throw Error('synthetic');}});
          await restore.bind({identity:'owner',conversationId:'synthetic_chat',contextVersion:'epoch:1'});
          if(restore.getState().phase!=='ready')throw Error('not restored');
        }''')
        self.assertEqual(self.page.evaluate('calls'),0)
        self.page.get_by_role('button',name='核对原请求').click()
        self.page.get_by_role('status').filter(has_text='原请求已保留').wait_for()
        self.assertEqual(self.page.evaluate('calls'),1)

    def test_reference_scope_conflict_and_explicit_account_cleanup(self):
        result=self.page.evaluate('''async()=>{
          const {createRecoveryReferenceStore}=await import('/lifecycle_recovery_reference_store.mjs');
          const store=createRecoveryReferenceStore({enabled:true,indexedDB});
          await Promise.all([store.save('owner',ref),store.save('owner',ref)]);
          let conflict;try{await store.save('owner',{...ref,attempt_request_id:'other'});}catch(e){conflict=e.message;}
          const old=await store.load('owner',ref.conversation_id),absent=await store.load('other',ref.conversation_id);
          await store.save('other',ref);await store.clearIdentity('owner');
          const cleared=await store.load('owner',ref.conversation_id),remaining=await store.load('other',ref.conversation_id);
          await store.remove('other',ref.conversation_id);
          return {conflict,old,absent,cleared,remaining,removed:await store.load('other',ref.conversation_id)};
        }''')
        self.assertEqual(result['conflict'],'reference_conflict');self.assertEqual(result['old'],result['remaining'])
        for key in ('absent','cleared','removed'):self.assertIsNone(result[key])

    def test_corrupt_reference_is_not_repaired_or_treated_as_success(self):
        result=self.page.evaluate('''async()=>{
          const {createRecoveryReferenceStore}=await import('/lifecycle_recovery_reference_store.mjs');
          const store=createRecoveryReferenceStore({enabled:true,indexedDB});await store.save('owner',ref);
          await new Promise((resolve,reject)=>{
            const r=indexedDB.open('guiting-lifecycle-recovery-v1',1);r.onsuccess=()=>{
              const db=r.result,t=db.transaction('pendingReferences','readwrite');
              t.objectStore('pendingReferences').put({version:2,reference:ref},['owner',ref.conversation_id]);
              t.oncomplete=()=>{db.close();resolve();};t.onabort=reject;
            };
          });
          let load,save;try{await store.load('owner',ref.conversation_id);}catch(e){load=e.message;}
          try{await store.save('owner',ref);}catch(e){save=e.message;}
          return {load,save};
        }''')
        self.assertEqual(result,{'load':'reference_storage_unavailable','save':'reference_storage_unavailable'})

    def test_default_reference_store_has_no_indexeddb_io(self):
        value=self.page.evaluate('''async()=>{
          const {createRecoveryReferenceStore}=await import('/lifecycle_recovery_reference_store.mjs');
          let calls=0;const store=createRecoveryReferenceStore({indexedDB:{open(){calls++;throw Error('forbidden');}}});
          await store.save(null,null);await store.load(null,null);await store.remove(null,null);await store.clearIdentity(null);
          return calls;
        }''')
        self.assertEqual(value,0)

    def test_restore_disabled_same_turn_cancellation_and_sibling_ownership(self):
        result=self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          const root=document.querySelector('#root');const sibling=document.createElement('p');sibling.textContent='keep';root.append(sibling);
          let loads=0;const c=mountRecoveryRestore(root,{loadReference:async()=>{loads++;return ref;},observe:async()=>{calls++;return reply();}});
          await c.bind(null);const off=root.querySelectorAll('section').length;
          c.setEnabled(true);const pending=c.bind({identity:'owner',conversationId:ref.conversation_id,contextVersion:'v1'});
          c.setEnabled(false);await pending;c.setEnabled(true);
          const idle=c.getState().phase,buttonDisabled=root.querySelector('button').disabled;
          c.dispose();c.setEnabled(true);await c.bind({});
          return {off,loads,calls,idle,buttonDisabled,children:root.children.length,text:root.textContent,phase:c.getState().phase};
        }''')
        self.assertEqual(result,{'off':0,'loads':0,'calls':0,'idle':'idle','buttonDisabled':True,
                                 'children':1,'text':'keep','phase':'disabled'})

    def test_restore_reverse_completion_and_aba_never_bind_old_context(self):
        result=self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          const jobs=[];let last;
          const c=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
            loadReference:(owner,chat,{signal})=>new Promise((resolve,reject)=>jobs.push({owner,chat,signal,resolve,reject})),
            observe:async r=>{calls++;last=r;return reply();}});
          const a={identity:'account:connection1',conversationId:ref.conversation_id,contextVersion:'v1'};
          const first=c.bind(a);await Promise.resolve();a.identity='mutated';
          const second=c.bind({identity:'account:connection2',conversationId:'other_chat',contextVersion:'v2'});await Promise.resolve();
          const third=c.bind({identity:'account:connection1',conversationId:ref.conversation_id,contextVersion:'v1'});await Promise.resolve();
          jobs[2].resolve({...ref,attempt_request_id:'latest'});await third;
          jobs[0].resolve({...ref,attempt_request_id:'stale'});await first;
          jobs[1].reject(Error('<img src=x onerror=alert(1)>'));await second;
          document.querySelector('button').click();await new Promise(resolve=>setTimeout(resolve,0));
          return {phase:c.getState().phase,attempt:last.attempt_request_id,calls,
            aborted:jobs.slice(0,2).every(j=>j.signal.aborted),owner:jobs[0].owner,
            raw:document.body.textContent.includes('<img'),images:document.querySelectorAll('img').length};
        }''')
        self.assertEqual(result,{'phase':'ready','attempt':'latest','calls':1,'aborted':True,
                                 'owner':'account:connection1','raw':False,'images':0})
        self.page.get_by_role('status').filter(has_text='原请求曾完成激活确认。').wait_for()
        self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth>innerWidth'))

    def test_restore_each_scope_dimension_invalidates_old_load(self):
        result=self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          const results=[];
          for(const delta of [{identity:'other:connection'},{conversationId:'other_chat'},{contextVersion:'v2'}]){
            const jobs=[];const c=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
              loadReference:()=>new Promise(resolve=>jobs.push(resolve)),observe:async()=>{calls++;return reply();}});
            const a={identity:'owner:connection',conversationId:ref.conversation_id,contextVersion:'v1'};
            const old=c.bind(a);await Promise.resolve();
            const next=c.bind({...a,...delta});await Promise.resolve();
            jobs[0](ref);await old;
            const blocked=document.querySelector('button').disabled,phase=c.getState().phase;
            jobs[1]({...ref,conversation_id:delta.conversationId||ref.conversation_id});await next;
            results.push({blocked,phase,ready:c.getState().phase});c.dispose();
          }
          return {results,calls};
        }''')
        self.assertEqual(result,{'results':[{'blocked':True,'phase':'loading','ready':'ready'}]*3,'calls':0})

    def test_restore_clear_off_dispose_ignore_late_success_and_failure(self):
        result=self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          const outcomes=[];
          for(const action of ['clear','off','dispose'])for(const rejected of [false,true]){
            let finish,fail,signal;const c=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
              loadReference:(_,__,o)=>{signal=o.signal;return new Promise((resolve,reject)=>{finish=resolve;fail=reject;});},
              observe:async()=>{calls++;return reply();}});
            const pending=c.bind({identity:'owner',conversationId:ref.conversation_id,contextVersion:'v1'});await Promise.resolve();
            if(action==='off')c.setEnabled(false);else c[action]();
            const snapshot=c.getState();if(rejected)fail(Error('private failure'));else finish(ref);await pending;
            outcomes.push({same:snapshot===c.getState(),aborted:signal.aborted,
              enabledButtons:[...document.querySelectorAll('button')].filter(b=>!b.disabled).length,
              sections:document.querySelectorAll('section').length});c.dispose();
          }
          return {outcomes,calls};
        }''')
        expected=[{'same':True,'aborted':True,'enabledButtons':0,'sections':count}
                  for count in (2,2,0,0,0,0)]
        self.assertEqual(result,{'outcomes':expected,'calls':0})

    def test_restore_missing_invalid_rejected_and_wrong_chat_never_bind(self):
        result=self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          const phases=[];
          for(const value of [null,undefined,{...ref,extra:true},{...ref,conversation_id:'wrong'},'reject']){
            const c=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
              loadReference:async()=>{if(value==='reject')throw Error('<img src=x onerror=alert(1)>');return value;},
              observe:async()=>{calls++;return reply();}});
            await c.bind({identity:'owner',conversationId:ref.conversation_id,contextVersion:'v1'});
            phases.push([c.getState().phase,document.querySelector('button').disabled]);c.dispose();
          }
          return {phases,calls,images:document.querySelectorAll('img').length};
        }''')
        self.assertEqual(result,{'phases':[['missing',True]]+[['unavailable',True]]*4,'calls':0,'images':0})

    def test_restore_invalid_binding_withdraws_old_reference_without_load(self):
        result=self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');let loads=0;
          const c=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
            loadReference:async()=>{loads++;return ref;},observe:async()=>{calls++;return reply();}});
          const scope={identity:'owner',conversationId:ref.conversation_id,contextVersion:'v1'};
          await c.bind(scope);const phases=[];
          for(const invalid of [null,{...scope,contextVersion:''},{...scope,identity:''},
              {...scope,conversationId:'../chat'},{...scope,extra:true}]){
            await c.bind(invalid);phases.push([c.getState().phase,document.querySelector('button').disabled]);
          }
          return {loads,calls,phases};
        }''')
        self.assertEqual(result,{'loads':1,'calls':0,'phases':[['invalid_context',True]]*5})

    def test_restore_pending_query_switch_and_revocation_require_explicit_binding(self):
        self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          window.queryJobs=[];window.loads=0;
          window.restore=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
            loadReference:async()=>{loads++;return ref;},observe:(_,o)=>{calls++;return new Promise(resolve=>queryJobs.push({resolve,signal:o.signal}));}});
          window.scope={identity:'owner',conversationId:ref.conversation_id,contextVersion:'v1'};
          await restore.bind(scope);
        }''')
        button=self.page.get_by_role('button',name='核对原请求');button.click()
        self.page.evaluate('''async()=>{
          await restore.bind({...scope,contextVersion:'v2'});queryJobs[0].resolve(reply());
          await Promise.resolve();await Promise.resolve();
        }''')
        self.assertTrue(self.page.evaluate('queryJobs[0].signal.aborted'))
        self.page.get_by_role('status').filter(has_text='尚未核对。').wait_for()
        button.click();self.page.evaluate('queryJobs[1].resolve({status:403})')
        self.page.get_by_role('status').filter(has_text='身份或权限已变化').wait_for()
        self.assertTrue(button.is_disabled());self.page.evaluate('restore.setEnabled(true)')
        self.assertEqual(self.page.evaluate('[calls,loads]'),[2,2]);self.assertTrue(button.is_disabled())
        self.page.evaluate('restore.bind({...scope,contextVersion:"reauthorized"})')
        self.page.get_by_role('status').filter(has_text='尚未核对。').wait_for()
        self.assertFalse(button.is_disabled());self.assertEqual(self.page.evaluate('[calls,loads]'),[2,3])

    def test_restore_query_singleflight_and_disabling_rejects_old_query(self):
        self.page.evaluate('''async()=>{
          const {mountRecoveryRestore}=await import('/lifecycle_recovery_restore.mjs');
          window.restore=mountRecoveryRestore(document.querySelector('#root'),{enabled:true,
            loadReference:async()=>ref,observe:(_,o)=>{calls++;window.querySignal=o.signal;return new Promise(resolve=>window.finishQuery=resolve);}});
          await restore.bind({identity:'owner',conversationId:ref.conversation_id,contextVersion:'v1'});
          const b=document.querySelector('button');b.click();b.click();b.click();
        }''')
        self.assertEqual(self.page.evaluate('calls'),1)
        self.assertEqual(self.page.get_by_role('region',name='接续结果核对',exact=True).get_attribute('aria-busy'),'true')
        self.page.evaluate('''async()=>{
          restore.setEnabled(false);restore.setEnabled(true);finishQuery(reply());await Promise.resolve();await Promise.resolve();
        }''')
        self.assertTrue(self.page.evaluate('querySignal.aborted'))
        self.assertTrue(self.page.get_by_role('button',name='核对原请求').is_disabled())
        self.assertEqual(self.page.locator('button').count(),1)
        self.assertNotIn('原请求曾完成激活确认。',self.page.locator('#root').inner_text())

if __name__=='__main__':unittest.main()
