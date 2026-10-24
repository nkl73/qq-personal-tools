const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../extensions/chatgpt/worker.js'),'utf8');
async function main() {
  const states=[{status:'loading',url:'about:blank'},{status:'loading',url:'https://chatgpt.com/'},{id:42,status:'complete',url:'https://chatgpt.com/',title:'ChatGPT'}];
  let created=null,pings=0;
  const context={URL,importScripts:()=>{},crypto:{randomUUID:()=> 'fixture-browser-instance'},
    setTimeout:callback=>queueMicrotask(callback),
    chrome:{tabs:{get:async()=>states.shift()||{id:42,status:'complete',url:'https://chatgpt.com/'},
      create:async options=>{created=options;return {id:42};},
      sendMessage:async()=>{pings++;return {ok:true};}},
      storage:{local:{get:async()=>({enabled:false,instance:'fixture-browser-instance'}),set:async()=>{}}},
      runtime:{onMessage:{addListener:()=>{}}},alarms:{onAlarm:{addListener:()=>{}},create:async()=>{}}}};
  vm.createContext(context);vm.runInContext(source,context);
  const result=await context.perform({action:'new',payload:{}});
  assert.equal(result.tab.id,42);assert.equal(created.active,false);assert.equal(pings,1);
  assert.equal(context.allowed('https://chatgpt.com/?private=x'),false);
  assert.equal(context.allowed('https://chatgpt.com.evil/'),false);
  await assert.rejects(context.perform({action:'send',payload:{tab_id:42,expected_url:'https://chatgpt.com/c/other',text:'fixture'}}),/网页已切换/);
  assert.equal(pings,2); // Only readiness ping, no send to the wrong conversation.
  context.chrome.tabs.get=async()=>({status:'complete',url:'https://example.com/'});
  await assert.rejects(context.tabReady(42),/离开普通 ChatGPT/);
  console.log('4 browser worker cases passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
