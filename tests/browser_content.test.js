const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../extensions/chatgpt/content.js'),'utf8');
async function scenario(mode) {
  let text=mode==='draft'?'unsent draft':'',clicks=0,submitted=false;
  class TextArea {get value(){return text;}set value(v){text=v;}}
  const editor=new TextArea();editor.offsetParent={};editor.dispatchEvent=()=>{};
  const button={offsetParent:{},disabled:false,getAttribute:()=>'',click:()=>{clicks++;submitted=true;}};
  const user={innerText:mode==='wrong-user'?'another input':'fixture question'};
  const modern=mode.startsWith('modern');
  const answer={innerText:'fixture answer',closest:()=>modern?null:({querySelector:()=>mode==='no-completion'?null:{}})};
  answer.parentElement={querySelector:()=>modern?{}:null,contains:node=>node===answer,parentElement:null};
  const realUser={querySelector:selector=>selector.startsWith('div')?user:null};
  const optimisticUser={querySelector:selector=>selector.startsWith('span')?{}:null};
  const context={setTimeout:callback=>{queueMicrotask(callback);},HTMLTextAreaElement:TextArea,InputEvent:class{},
    location:{href:'https://chatgpt.com/c/existing'},
    chrome:{runtime:{onMessage:{addListener:()=>{}},sendMessage:async()=>({active:mode!=='cancelled'})}},
    document:{querySelector:()=>mode==='busy'?{}:null,querySelectorAll:selector=>{
      if(selector==='#prompt-textarea')return [editor];
      if(selector.includes('stop-button'))return mode==='busy'?[{offsetParent:{}}]:[];
      if(selector.includes('data-message-author-role="user"'))return modern?[]:submitted?[user]:[];
      if(selector.includes('data-message-author-role="assistant"'))return modern?[]:submitted?[answer]:[];
      if(selector==='.bg-user-message')return modern&&submitted?(mode==='modern-optimistic'?[optimisticUser,realUser]:[user]):[];
      if(selector.includes('MarkdownRoot-'))return modern&&submitted?[answer]:[];
      if(selector.startsWith('button'))return mode==='no-submit'?[]:[button];
      return [];
    }}};
  vm.createContext(context);vm.runInContext(source,context);
  const result=await context.send({id:'fixture-job',payload:{expected_url:context.location.href,text:'fixture question'}});
  return {result,clicks,text};
}
(async()=>{
  let value=await scenario('success');assert.equal(value.result.ok,true);assert.equal(value.result.text,'fixture answer');assert.equal(value.clicks,1);
  value=await scenario('modern');assert.equal(value.result.ok,true);assert.equal(value.result.text,'fixture answer');assert.equal(value.clicks,1);
  value=await scenario('modern-optimistic');assert.equal(value.result.ok,true);assert.equal(value.clicks,1);
  for(const mode of ['draft','busy','cancelled','no-submit']){value=await scenario(mode);assert.equal(value.result.ok,false,mode);assert.equal(value.clicks,0,mode);}
  value=await scenario('wrong-user');assert.equal(value.result.ok,false);assert.equal(value.clicks,1);
  value=await scenario('no-completion');assert.equal(value.result.ok,false);assert.equal(value.clicks,1);
  console.log('9 browser DOM cases passed');
})().catch(error=>{console.error(error);process.exit(1);});
