import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRecoveryController} from '../../examples/lifecycle_recovery_client.mjs';
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
