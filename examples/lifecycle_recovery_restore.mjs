import {validateRecoveryReference} from './lifecycle_recovery_client.mjs';
import {mountRecoveryPanel} from './lifecycle_recovery_panel.mjs';

const messages=Object.freeze({
  idle:'请先选择需要恢复的会话。',
  loading:'正在读取本地原请求引用…',
  ready:'本地引用已载入。其存在不证明原请求已执行。',
  missing:'本地未找到原请求引用。发送结果仍需另行核对。',
  unavailable:'本地原请求引用未能读取。发送结果仍需另行核对。',
  invalid_context:'会话信息不完整，请重新绑定。',
});

function context(value) {
  const keys=['identity','conversationId','contextVersion'];
  if(!value||typeof value!=='object'||Array.isArray(value)
      ||Object.keys(value).length!==keys.length||!keys.every(key=>Object.hasOwn(value,key))
      ||typeof value.identity!=='string'||!value.identity||value.identity.length>256
      ||typeof value.contextVersion!=='string'||!value.contextVersion||value.contextVersion.length>256
      ||typeof value.conversationId!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value.conversationId)) {
    throw Error('invalid_restore_context');
  }
  return Object.freeze(Object.fromEntries(keys.map(key=>[key,value[key]])));
}

// Host calls bind on every account/connection/chat/version change. No discovery,
// polling, storage writes or automatic status query. loadReference is normally
// (identity, conversationId) => store.load(identity, conversationId).
export function mountRecoveryRestore(root,{enabled=false,loadReference,observe}={}) {
  let active=false,disposed=false,generation=0,scope=null,abort=null;
  let section=null,status=null,panel=null;
  let view=Object.freeze({phase:'disabled'});
  function publish(phase) {
    view=Object.freeze({phase});
    if(status)status.textContent=messages[phase];
    if(section)section.setAttribute('aria-busy',String(phase==='loading'));
    return view;
  }
  function invalidate() {
    generation++;scope=null;abort?.abort();abort=null;panel?.clear();
  }
  function clear() {invalidate();return publish(active&&!disposed?'idle':'disabled');}
  function setEnabled(value) {
    if(disposed)return view;
    if(value!==true) {
      active=false;invalidate();panel?.dispose();panel=null;
      section?.remove();section=null;status=null;return publish('disabled');
    }
    if(active)return view;
    if(!root?.ownerDocument||typeof root.append!=='function')throw Error('invalid_restore_root');
    if(typeof loadReference!=='function'||typeof observe!=='function')throw Error('invalid_restore_dependencies');
    const document=root.ownerDocument;
    section=document.createElement('section');section.setAttribute('aria-label','本地接续引用恢复');
    status=document.createElement('p');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    const panelRoot=document.createElement('div');section.append(status,panelRoot);
    panel=mountRecoveryPanel(panelRoot,{enabled:true,observe});root.append(section);
    active=true;return publish('idle');
  }
  async function bind(value) {
    clear();
    if(!active||disposed)return view;
    try{scope=context(value);}catch{return publish('invalid_context');}
    const token=generation,bound=scope;
    abort=new AbortController();const signal=abort.signal;
    const current=()=>active&&!disposed&&generation===token&&scope===bound;
    publish('loading');
    // A same-turn clear/disable must prevent even starting the load.
    await Promise.resolve();
    if(!current())return view;
    try {
      const value=await loadReference(bound.identity,bound.conversationId,{signal});
      if(!current())return view;
      if(value===null)return publish('missing');
      const original=validateRecoveryReference(value);
      if(original.conversation_id!==bound.conversationId)throw Error('reference_scope_mismatch');
      panel.bind(bound.identity,original);
      return publish('ready');
    } catch {return current()?publish('unavailable'):view;}
    finally {if(current())abort=null;}
  }
  const api=Object.freeze({getState:()=>view,bind,clear,setEnabled,
    dispose(){if(disposed)return view;setEnabled(false);disposed=true;return view;},
  });
  setEnabled(enabled);return api;
}
