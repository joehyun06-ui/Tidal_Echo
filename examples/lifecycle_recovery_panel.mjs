import {createRecoveryController} from './lifecycle_recovery_client.mjs';

const messages = Object.freeze({idle:'请先绑定原请求。',ready:'尚未核对。',checking:'正在核对原请求…',
  attempt_missing:'未找到原尝试记录。发送结果仍需核对，请保留原请求。',
  unconfirmed:'原尝试已有登记，尚无持久化激活确认。',historical_confirmed:'原请求曾完成激活确认。',
  unavailable:'本次核对未完成。原请求已保留。',revoked:'身份或权限已变化，请重新核对连接。'});

export function mountRecoveryPanel(root, {enabled=false, observe}={}) {
  if (enabled !== true) return Object.freeze({bind(){},clear(){},dispose(){}});
  if (!root?.ownerDocument || typeof root.append !== 'function') throw Error('invalid_panel_root');
  const controller=createRecoveryController({enabled:true,observe});
  const document=root.ownerDocument, section=document.createElement('section');
  section.setAttribute('aria-label','接续结果核对');
  const title=document.createElement('h3');title.textContent='接续结果核对';
  const status=document.createElement('p');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
  const note=document.createElement('p');
  note.textContent='这里显示原请求的历史结果。当前窗口可用性仍需单独确认。';
  const button=document.createElement('button');button.type='button';button.textContent='核对原请求';
  section.append(title,status,note,button);root.append(section);let disposed=false;
  function render() {
    if (disposed) return;
    const view=controller.getState();status.textContent=messages[view.phase] ?? messages.unavailable;
    section.setAttribute('aria-busy',String(view.phase==='checking'));
    button.disabled=!view.reference || view.phase==='checking';
  }
  async function check() {
    if (disposed || button.disabled) return;
    const pending=controller.check();render();await pending;render();
  }
  button.addEventListener('click',check);render();
  return Object.freeze({
    bind(identity,reference) {try{return controller.bind(identity,reference);}finally{render();}},
    clear(){controller.clear();render();},
    dispose(){if(disposed)return;disposed=true;controller.dispose();button.removeEventListener('click',check);section.remove();},
  });
}
