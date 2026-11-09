import {validateRecoveryReference} from './lifecycle_recovery_client.mjs';
const STORE='pendingReferences';
const fail=(code='reference_storage_unavailable')=>new Error(code);
function identity(value) {
  if(typeof value!=='string'||!value||value.length>256)throw fail('invalid_identity');
  return value;
}
function decode(value) {
  if(!value||Object.keys(value).length!==2||value.version!==1||!Object.hasOwn(value,'reference'))throw fail();
  try{return validateRecoveryReference(value.reference);}catch{throw fail();}
}

export function createRecoveryReferenceStore({enabled=false,indexedDB,databaseName='guiting-lifecycle-recovery-v1'}={}) {
  const active=enabled===true;
  if(active&&(!indexedDB||typeof indexedDB.open!=='function'||typeof databaseName!=='string'||!databaseName))throw fail();
  function open() {
    return new Promise((resolve,reject)=>{
      let done=false,request;
      const deny=()=>{if(!done){done=true;reject(fail());}};
      try{request=indexedDB.open(databaseName,1);}catch{deny();return;}
      request.onblocked=deny;request.onerror=deny;
      request.onupgradeneeded=event=>{
        if(event.oldVersion===0)request.result.createObjectStore(STORE);
        else{request.transaction.abort();deny();}
      };
      request.onsuccess=()=>{
        const db=request.result;
        if(done){db.close();return;}
        db.onversionchange=()=>db.close();
        if(db.objectStoreNames.length!==1||!db.objectStoreNames.contains(STORE)){db.close();deny();return;}
        done=true;resolve(db);
      };
    });
  }
  async function transaction(mode,operation) {
    const db=await open();
    return new Promise((resolve,reject)=>{
      let tx,result=null,error=fail();
      try{
        tx=db.transaction(STORE,mode);const store=tx.objectStore(STORE);
        tx.oncomplete=()=>{db.close();resolve(result);};
        tx.onabort=()=>{db.close();reject(error);};
        tx.onerror=()=>{};
        if(store.keyPath!==null||store.autoIncrement||store.indexNames.length){tx.abort();return;}
        operation(store,value=>{result=value;},caught=>{error=caught;tx.abort();});
      }catch{if(tx)tx.abort();else{db.close();reject(error);}}
    });
  }
  return Object.freeze({
    async save(account,original) {
      if(!active)return null;
      const owner=identity(account),ref=validateRecoveryReference(original),key=[owner,ref.conversation_id];
      return transaction('readwrite',(store,set,abort)=>{
        const request=store.get(key);
        request.onsuccess=()=>{
          try{
            if(request.result!==undefined){
              const old=decode(request.result);
              if(JSON.stringify(old)!==JSON.stringify(ref))throw fail('reference_conflict');
            }else store.add({version:1,reference:ref},key);
            set(ref);
          }catch(error){abort(error);}
        };
      });
    },
    async load(account,conversationId) {
      if(!active)return null;
      const owner=identity(account);
      if(typeof conversationId!=='string'||!conversationId||conversationId.length>160)throw fail('invalid_reference');
      return transaction('readonly',(store,set,abort)=>{
        const request=store.get([owner,conversationId]);
        request.onsuccess=()=>{
          try{
            const value=request.result===undefined?null:decode(request.result);
            if(value&&value.conversation_id!==conversationId)throw fail();
            set(value);
          }catch(error){abort(error);}
        };
      });
    },
    async remove(account,conversationId) {
      if(!active)return null;
      const owner=identity(account);
      if(typeof conversationId!=='string'||!conversationId||conversationId.length>160)throw fail('invalid_reference');
      return transaction('readwrite',store=>store.delete([owner,conversationId]));
    },
    async clearIdentity(account) {
      if(!active)return null;
      const owner=identity(account);
      return transaction('readwrite',store=>{
        const cursor=store.openCursor();cursor.onsuccess=()=>{
          const row=cursor.result;if(!row)return;
          if(Array.isArray(row.key)&&row.key[0]===owner)row.delete();
          row.continue();
        };
      });
    },
  });
}
