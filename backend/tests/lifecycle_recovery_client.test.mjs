import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRecoveryController} from '../../examples/lifecycle_recovery_client.mjs';
import {createRecoveryStatusTransport} from '../../examples/lifecycle_recovery_transport.mjs';
const ref = Object.freeze({conversation_id:'synthetic_chat', activation_request_id:'activate_1',
  attempt_request_id:'attempt_1',packet_sha256:'a'.repeat(64)});
const body = (state = 'historical_confirmed') => ({ok:true,state,historical:state === 'historical_confirmed',
  revision_applied:state === 'historical_confirmed' ? 3 : null,
  window_confirmed_at_apply:state === 'historical_confirmed',current_window_confirmed:false,
  reply_completed:false,retry_authorized:false});
const tick = () => Promise.resolve();

test('default off and binding never call transport', async () => {
  let calls=0;const c=createRecoveryController({observe:()=>calls++});c.bind('owner',ref);await c.check();
  assert.equal(calls,0);assert.equal(c.getState().phase,'disabled');
});
test('explicit check keeps exact original reference and deduplicates pending calls', async () => {
  let finish,calls=0;const c=createRecoveryController({enabled:true,observe:(value)=>{
    calls++;assert.deepEqual(value,ref);assert(Object.isFrozen(value));return new Promise(r=>finish=r);}});
  const mutable={...ref};c.bind('owner',mutable);mutable.attempt_request_id='changed';
  assert.equal(calls,0);const first=c.check();assert.equal(c.check(),first);await tick();
  finish({status:200,body:body()});await first;assert.equal(calls,1);
  assert.equal(c.getState().phase,'historical_confirmed');assert.equal(c.getState().currentWindowConfirmed,false);
  assert.equal(c.getState().retryAuthorized,false);
});
test('all three response states retain conservative meaning', async () => {
  for(const state of ['attempt_missing','unconfirmed','historical_confirmed']) {
    const c=createRecoveryController({enabled:true,observe:async()=>({status:200,body:body(state)})});
    c.bind('owner',ref);await c.check();assert.equal(c.getState().phase,state);
    assert.equal(c.getState().retryAuthorized,false);
  }
});
test('malformed or overclaiming responses never display confirmation', async () => {
  for(const value of [{...body(),retry_authorized:true},{...body(),current_window_confirmed:true},
      {...body(),revision_applied:3.5},{...body(),historical:false},{...body(),extra:'secret'},
      {...body('attempt_missing'),revision_applied:3}]) {
    const c=createRecoveryController({enabled:true,observe:async()=>({status:200,body:value})});
    c.bind('owner',ref);await c.check();assert.equal(c.getState().phase,'unavailable');
    assert.deepEqual(c.getState().reference,ref);
  }
});
test('network/conflict errors preserve reference without automatic retry', async () => {
  for(const response of [null,{status:409},{status:503}]) {
    let calls=0;const c=createRecoveryController({enabled:true,observe:async()=>{
      calls++;if(!response)throw Error('private transport error');return response;}});
    c.bind('owner',ref);await c.check();await tick();assert.equal(calls,1);
    assert.equal(c.getState().phase,'unavailable');assert.deepEqual(c.getState().reference,ref);
    assert(!JSON.stringify(c.getState()).includes('private transport'));
  }
});
test('identity change, clear and dispose abort and ignore late success', async () => {
  for(const action of ['bind','clear','dispose']) {
    let finish,signal;const c=createRecoveryController({enabled:true,observe:(_,options)=>{
      signal=options.signal;return new Promise(r=>finish=r);}});
    c.bind('old-owner',ref);const pending=c.check();await tick();
    if(action==='bind')c.bind('new-owner',{...ref,conversation_id:'other_chat'});else c[action]();
    const expected=c.getState();assert(signal.aborted);finish({status:200,body:body()});await pending;
    assert.equal(c.getState(),expected);assert.notEqual(c.getState().phase,'historical_confirmed');
  }
});
test('revocation clears scoped reference and requires explicit rebinding', async () => {
  for(const status of [401,403]) {
    let calls=0;const c=createRecoveryController({enabled:true,observe:async()=>{calls++;return {status};}});
    c.bind('owner',ref);await c.check();assert.equal(c.getState().phase,'revoked');
    assert.equal(c.getState().reference,null);await c.check();assert.equal(calls,1);
  }
});
test('invalid rebinding withdraws previous result and never sends', async () => {
  const c=createRecoveryController({enabled:true,observe:async()=>({status:200,body:body()})});
  c.bind('owner',ref);await c.check();assert.throws(()=>c.bind('owner',{...ref,dispatch:true}),/invalid_reference/);
  assert.equal(c.getState().phase,'idle');assert.equal(c.getState().reference,null);
  assert.throws(()=>c.bind('owner',{...ref,conversation_id:undefined}),/invalid_reference/);
  c.bind('owner',{...ref,attempt_request_id:'attempt:1'});
  assert.equal(c.getState().phase,'ready');
});

const response=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
test('status transport default off never validates or calls host request',async()=>{
  let calls=0;const observe=createRecoveryStatusTransport({request:()=>calls++});
  assert.deepEqual(await observe(null),{status:503});assert.equal(calls,0);
  assert.throws(()=>createRecoveryStatusTransport({enabled:true,request(){},timeoutMs:10001}));
});
test('status transport uses fixed encoded session path and exact copied reference',async()=>{
  let sent;const observe=createRecoveryStatusTransport({enabled:true,request:async(path,options)=>{
    sent={path,options};return response(body());}});
  const mutable={...ref,conversation_id:'chat:with.period'},task=observe(mutable);
  mutable.attempt_request_id='mutated';const result=await task;
  assert.equal(sent.path,'/app/sessions/chat%3Awith.period/lifecycle/activation/status');
  assert.equal(sent.options.method,'POST');assert.equal(sent.options.redirect,'error');
  assert.equal(sent.options.cache,'no-store');assert.deepEqual(sent.options.headers,{'Content-Type':'application/json'});
  assert.equal(JSON.parse(sent.options.body).attempt_request_id,ref.attempt_request_id);
  assert.equal(result.status,200);assert(Object.isFrozen(result.body));
  assert.equal(result.body.current_window_confirmed,false);assert.equal(result.body.retry_authorized,false);
});
test('invalid reference is rejected before any status IO',async()=>{
  let calls=0;const observe=createRecoveryStatusTransport({enabled:true,request:()=>calls++});
  await assert.rejects(observe({...ref,dispatch:true}),/invalid_reference/);assert.equal(calls,0);
});
test('error statuses cancel unread bodies and client 401 maps to revocation',async()=>{
  for(const status of [401,403,409,503,302,500]){
    let cancelled=0;
    const observe=createRecoveryStatusTransport({enabled:true,request:async()=>({status,body:{cancel(){cancelled++;}}})});
    assert.deepEqual(await observe(ref),{status:[401,403,409,503].includes(status)?status:502});
    assert.equal(cancelled,1);
  }
  const observe=createRecoveryStatusTransport({enabled:true,request:async()=>{throw Error('401');}});
  const c=createRecoveryController({enabled:true,observe});c.bind('owner',ref);await c.check();
  assert.equal(c.getState().phase,'revoked');assert.equal(c.getState().reference,null);
});
test('bounded status parser rejects oversized, malformed, redirected and overclaiming replies',async()=>{
  const cases=[()=>new Response('x'.repeat(4097),{headers:{'content-type':'application/json'}}),
    ()=>new Response('{}',{headers:{'content-type':'application/json','content-length':'4097'}}),
    ()=>new Response('{}',{headers:{'content-type':'text/html'}}),
    ()=>new Response(new Uint8Array([255]),{headers:{'content-type':'application/json'}}),
    ()=>response({...body(),reply_completed:true}),()=>response({...body(),state:'unknown'}),
    ()=>({status:200,redirected:true,body:{cancel(){}}}),
    ()=>response({...body(),revision_applied:9007199254740992}),()=>response({...body(),extra:'private'})];
  for(const make of cases){
    const observe=createRecoveryStatusTransport({enabled:true,request:async()=>make()});
    await assert.rejects(observe(ref),error=>error.message==='status_unavailable');
  }
});
test('status deadline settles even if request ignores abort and closes a late response',async()=>{
  let finish,signal,calls=0,cancelled=0;
  const observe=createRecoveryStatusTransport({enabled:true,timeoutMs:5,request:(_,o)=>{
    signal=o.signal;calls++;return new Promise(resolve=>finish=resolve);}});
  await assert.rejects(observe(ref),/status_unavailable/);assert(signal.aborted);assert.equal(calls,1);
  finish({status:200,body:{cancel(){cancelled++;}}});await tick();await tick();
  assert.equal(cancelled,1);
});
test('abort before request or during body read prevents further effects',async()=>{
  const before=new AbortController();before.abort();let calls=0;
  const off=createRecoveryStatusTransport({enabled:true,request:async()=>{calls++;return response(body());}});
  await assert.rejects(off(ref,{signal:before.signal}),/status_unavailable/);assert.equal(calls,0);
  const during=new AbortController();let cancelled=0,read;
  const entered=new Promise(resolve=>read=resolve);
  const observe=createRecoveryStatusTransport({enabled:true,request:async()=>({status:200,
    headers:new Headers({'content-type':'application/json'}),body:{getReader:()=>({
      read(){read();return new Promise(()=>{});},cancel(){cancelled++;},releaseLock(){},
    })}})});
  const pending=observe(ref,{signal:during.signal});await entered;during.abort();
  await assert.rejects(pending,/status_unavailable/);assert.equal(cancelled,1);
});
test('transport rejection retains original reference and a manual retry is explicit',async()=>{
  let calls=0;const observe=createRecoveryStatusTransport({enabled:true,request:async()=>{
    calls++;if(calls===1)return response({...body(),current_window_confirmed:true});return response(body());}});
  const c=createRecoveryController({enabled:true,observe});c.bind('owner',ref);await c.check();
  assert.equal(c.getState().phase,'unavailable');assert.deepEqual(c.getState().reference,ref);
  await tick();assert.equal(calls,1);await c.check();assert.equal(calls,2);
  assert.equal(c.getState().phase,'historical_confirmed');assert.equal(c.getState().currentWindowConfirmed,false);
});
