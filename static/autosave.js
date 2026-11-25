/* Ordered writes; no browser persistence of personal data. */
(function(root) {
  class DraftAutosave {
    constructor({send,onState=()=>{},onSaved=()=>{},delay=1200,retryDelay=10000,timers=globalThis}) {
      Object.assign(this,{send,onState,onSaved,delay,retryDelay,timers});
      this.doc=null;this.timer=null;this.running=null;this.serial=0;this.saved=0;this.error=null;this.phase='idle';
    }
    get dirty(){return this.serial!==this.saved;}
    emit(phase){this.phase=phase;this.onState({phase,dirty:this.dirty,error:this.error});}
    cancelTimer(){if(this.timer!==null)this.timers.clearTimeout(this.timer);this.timer=null;}
    bind(doc){
      if(this.running)throw new Error('저장 중에는 문서를 바꿀 수 없습니다.');
      this.cancelTimer();this.doc=doc;this.serial=0;this.saved=0;this.error=null;this.emit(doc?'saved':'idle');
    }
    schedule(ms=this.delay){this.cancelTimer();this.timer=this.timers.setTimeout(()=>{this.timer=null;this.flush().catch(()=>{});},ms);}
    changed(){
      if(!this.doc)return;this.serial++;
      if(this.error?.status===409){this.emit('conflict');return;}
      this.error=null;this.emit('pending');this.schedule();
    }
    flush(){
      this.cancelTimer();
      if(this.running)return this.running;
      if(!this.doc||!this.dirty)return Promise.resolve();
      if(this.error?.status===409)return Promise.reject(this.error);
      this.running=this.drain().finally(()=>{this.running=null;});return this.running;
    }
    async drain(){
      while(this.dirty){
        const doc=this.doc,serial=this.serial;
        const body={version:doc.version,draft:JSON.parse(JSON.stringify(doc.draft))};this.emit('saving');
        try {
          const response=await this.send(doc.id,body);
          doc.version=response.version;doc.confirmedVersions=response.confirmedVersions;
          if(this.serial===serial)doc.results=response.results;
          this.saved=serial;this.error=null;this.onSaved(doc,this.serial===serial);this.emit(this.dirty?'pending':'saved');
        } catch(error) {
          this.error=error;this.emit(error.status===409?'conflict':'error');
          if(!error.status||error.status>=500)this.schedule(this.retryDelay);
          throw error;
        }
      }
    }
    async settle(){this.cancelTimer();if(this.running){try{await this.running;}catch{}}this.cancelTimer();}
  }
  if(typeof module!=='undefined'&&module.exports)module.exports={DraftAutosave};else root.DraftAutosave=DraftAutosave;
})(globalThis);
