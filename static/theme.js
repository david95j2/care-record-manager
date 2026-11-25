(function(){
  const key='activity-support-theme';let mode='system';
  try{mode=localStorage.getItem(key)||mode;}catch{}
  const media=matchMedia('(prefers-color-scheme: dark)');
  function apply(value){mode=['light','dark','system'].includes(value)?value:'system';document.documentElement.dataset.theme=mode==='system'?(media.matches?'dark':'light'):mode;}
  apply(mode);media.addEventListener('change',()=>apply(mode));
  document.addEventListener('DOMContentLoaded',()=>{
    const selector=document.getElementById('theme');selector.value=mode;
    selector.addEventListener('change',()=>{apply(selector.value);try{localStorage.setItem(key,mode);}catch{}});
  });
})();
