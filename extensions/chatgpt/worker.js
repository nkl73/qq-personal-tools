/* The generated config stays on this PC and is excluded from source archives. */
importScripts('config.js');
let polling=false,enabled=false,instance='',lastError='',resultChain=Promise.resolve();
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function allowed(url) {
  try {const u=new URL(url);return u.origin==='https://chatgpt.com' && !u.search && !u.hash && (u.pathname==='/' || /^\/c\/[a-zA-Z0-9-]+$/.test(u.pathname));}catch{return false;}
}
async function local(path,data) {
  const response=await fetch(QQ_BRIDGE.url+path,{method:'POST',headers:{'Authorization':'Bearer '+QQ_BRIDGE.token,'Content-Type':'application/json'},body:JSON.stringify({instance,...data}),signal:AbortSignal.timeout(8000)});
  if (!response.ok) throw new Error(response.status===409?'另一个浏览器已连接，请先断开它。':'本机面板未就绪（'+response.status+'）');
  return response.json();
}
async function tabReady(id) {
  for(let i=0;i<30;i++) {
    const tab=await chrome.tabs.get(id);
    if(tab.status==='loading' && (!tab.url || tab.url==='about:blank')) {await pause(500);continue;}
    if(!allowed(tab.url)) throw new Error('标签页已离开普通 ChatGPT 聊天，未发送。');
    if(tab.status==='complete') {
      try {const result=await chrome.tabs.sendMessage(id,{type:'qq-ping'});if(result?.ok)return tab;}catch{}
    }
    await pause(500);
  }
  throw new Error('ChatGPT 页面未就绪。请刷新已安装扩展的页面，确认能正常聊天。');
}
async function perform(job) {
  if(job.action==='list') {
    const tabs=await chrome.tabs.query({url:'https://chatgpt.com/*'});
    return {ok:true,tabs:tabs.filter(t=>allowed(t.url)).map(t=>({id:t.id,url:t.url,title:t.title||'ChatGPT'}))};
  }
  if(job.action==='new') {
    const tab=await chrome.tabs.create({url:'https://chatgpt.com/',active:false});
    const ready=await tabReady(tab.id);return {ok:true,tab:{id:ready.id,url:ready.url,title:ready.title||'ChatGPT'}};
  }
  if(job.action==='send') {
    const payload=job.payload;
    if(!Number.isInteger(payload.tab_id)||!allowed(payload.expected_url)||typeof payload.text!=='string'||!payload.text.trim()||payload.text.length>12000)throw new Error('网页请求格式无效。');
    const tab=await tabReady(payload.tab_id);
    if(tab.url!==payload.expected_url)throw new Error('网页已切换到另一段聊天，请重新选择；未发送。');
    return chrome.tabs.sendMessage(tab.id,{type:'qq-send',id:job.id,payload});
  }
  if(job.action==='inspect') {
    if(!Number.isInteger(job.payload.tab_id)||typeof job.payload.probe!=='string'||job.payload.probe.length>80)throw new Error('诊断请求格式无效。');
    const tab=await tabReady(job.payload.tab_id);return chrome.tabs.sendMessage(tab.id,{type:'qq-inspect',probe:job.payload.probe});
  }
  throw new Error('不支持的网页操作。');
}
async function flush() {
  const values=await chrome.storage.local.get({pending:{}});
  for(const [id,result] of Object.entries(values.pending)) {
    await local('/result',{id,result});delete values.pending[id];await chrome.storage.local.set({pending:values.pending});
  }
}
function storeResult(id,result) {
  resultChain=resultChain.catch(()=>{}).then(async()=>{
    const values=await chrome.storage.local.get({pending:{}});values.pending[id]=result;
    await chrome.storage.local.set({pending:values.pending});await flush();
  });
}
function queueFlush() {resultChain=resultChain.catch(()=>{}).then(flush);return resultChain;}
async function execute(job) {
  let result;
  try {result=await perform(job);if(!result?.ok)throw new Error(result?.error||'页面未确认操作结果。');}
  catch(error) {result={ok:false,error:String(error.message||error)+' 若已点击发送，不会自动重发，请先查看网页。'};}
  storeResult(job.id,result);
}
async function poll() {
  if(polling||!enabled)return;
  polling=true;
  try {
    while(enabled) {
      try {await queueFlush();const reply=await local('/next',{});lastError='';if(reply.job)void execute(reply.job);}
      catch(error){lastError=String(error.message||error);}
      await pause(2000);
    }
  }finally{polling=false;}
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(message.type==='active' && sender.tab && allowed(sender.tab.url)) {
    if(!enabled){reply({active:false});return;}
    local('/status',{id:message.id,progress:message.progress}).then(reply).catch(()=>reply({active:false}));return true;
  }
  if(sender.tab)return;
  if(message.type==='state'){reply({enabled,error:lastError});return;}
  if(message.type==='enable') {
    enabled=message.enabled===true;chrome.storage.local.set({enabled}).then(()=>{if(enabled)void poll();reply({ok:true});});return true;
  }
});
chrome.alarms.onAlarm.addListener(()=>{if(enabled)void poll();});
async function initialize() {
  const values=await chrome.storage.local.get({enabled:false,instance:''});enabled=values.enabled;
  instance=values.instance||crypto.randomUUID();await chrome.storage.local.set({instance});
  await chrome.alarms.create('qq-bridge',{periodInMinutes:.5});if(enabled)void poll();
}
initialize();
