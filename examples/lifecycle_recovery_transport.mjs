import {validateRecoveryReference,validateRecoveryStatus} from './lifecycle_recovery_client.mjs';

const LIMIT=4096;
const unavailable=()=>Error('status_unavailable');
const close=body=>{try{Promise.resolve(body?.cancel()).catch(()=>{});}catch{}};

// request is the host's captured authenticated API client, never a URL/token.
// No startup, retry, redirect following, credential storage or other endpoint.
export function createRecoveryStatusTransport({enabled=false,request,timeoutMs=10000}={}) {
  if(enabled!==true)return async()=>({status:503});
  if(typeof request!=='function'||!Number.isSafeInteger(timeoutMs)||timeoutMs<1||timeoutMs>10000) {
    throw Error('invalid_status_transport');
  }
  return async function observe(value,{signal}={}) {
    const original=validateRecoveryReference(value),controller=new AbortController();
    let response,reader,rejectStop;
    const stopped=new Promise((_,reject)=>{rejectStop=reject;});
    const stop=()=>{controller.abort();rejectStop(unavailable());};
    const timer=setTimeout(stop,timeoutMs);
    signal?.addEventListener('abort',stop,{once:true});
    if(signal?.aborted)stop();
    const race=task=>Promise.race([task,stopped]);
    try {
      const pending=Promise.resolve().then(()=>{
        if(controller.signal.aborted)throw unavailable();
        return request('/app/sessions/'+encodeURIComponent(original.conversation_id)+'/lifecycle/activation/status',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(original),
          signal:controller.signal,cache:'no-store',redirect:'error',
        });
      }).then(value=>{if(controller.signal.aborted){close(value?.body);throw unavailable();}return value;});
      response=await race(pending);
      if(response?.redirected||!Number.isInteger(response?.status))throw unavailable();
      if(response.status!==200)return {status:[401,403,409,503].includes(response.status)?response.status:502};
      if(response.headers?.get('content-type')?.trim().toLowerCase()!=='application/json'
          ||typeof response.body?.getReader!=='function')throw unavailable();
      const length=response.headers.get('content-length');
      if(length!==null&&(!/^[0-9]{1,8}$/.test(length)||Number(length)>LIMIT))throw unavailable();
      reader=response.body.getReader();const chunks=[];let size=0;
      for(;;) {
        const {done,value}=await race(reader.read());if(done)break;
        if(!(value instanceof Uint8Array)||size+value.byteLength>LIMIT)throw unavailable();
        size+=value.byteLength;chunks.push(value);
      }
      const bytes=new Uint8Array(size);let offset=0;
      for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}
      const body=validateRecoveryStatus(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)));
      return {status:200,body};
    } catch(error) {
      // GuiTingApi.request consumes 401 and throws this stable code after its
      // connection-owned logout callback. Other raw errors are never reflected.
      if(!controller.signal.aborted&&error?.message==='401')return {status:401};
      throw unavailable();
    } finally {
      clearTimeout(timer);signal?.removeEventListener('abort',stop);
      if(reader){try{Promise.resolve(reader.cancel()).catch(()=>{});reader.releaseLock();}catch{}}
      else close(response?.body);
    }
  };
}
