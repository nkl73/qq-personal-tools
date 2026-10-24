async function refresh() {
  const state=await chrome.runtime.sendMessage({type:'state'});
  document.querySelector('#state').textContent=state.enabled?'已启用。'+(state.error||'正在连接本机面板。'):'未连接。';
}
for (const [id,enabled] of [['connect',true],['disconnect',false]]) {
  document.getElementById(id).addEventListener('click',async()=>{
    await chrome.runtime.sendMessage({type:'enable',enabled});await refresh();
  });
}
refresh();
