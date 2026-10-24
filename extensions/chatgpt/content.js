/* DOM-only adapter. No cookies, network interception, keyboard injection or focus of browser windows. */
let busy=false;
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const normalized=text=>String(text||'').replace(/\s+/g,' ').trim();
const messages=role=>{
  const visible=el=>el.offsetParent!==null;
  const legacy=Array.from(document.querySelectorAll('[data-message-author-role="'+role+'"]')).filter(visible);
  if(legacy.length)return legacy;
  const rows=Array.from(document.querySelectorAll(role==='user'?'.bg-user-message':'[class*="MarkdownRoot-"]')).filter(visible);
  if(role==='user') {
    const committed=rows.map(el=>el.querySelector?.('div.whitespace-pre-wrap')).filter(Boolean);
    if(committed.length)return committed;
    return rows.filter(el=>!el.querySelector?.('span.leading-relaxed.whitespace-pre-wrap'));
  }
  return rows;
};
const stopButton=()=>Array.from(document.querySelectorAll('button[data-testid="stop-button"],button[aria-label="Stop generating"],button[aria-label="Stop"],button[aria-label="停止生成"],button[aria-label="停止"],button[aria-label="停止响应"]')).find(el=>el.offsetParent!==null);
const copySelector='button[data-testid="copy-turn-action-button"],button[aria-label="Copy"],button[aria-label="复制"]';
function completedText(answer,beforeAnswers) {
  const legacy=answer.closest('article');
  if(legacy?.querySelector(copySelector))return answer.innerText.trim();
  const current=messages('assistant');let container=answer.parentElement;
  for(let i=0;i<18 && container && container!==document.body;i++,container=container.parentElement) {
    if(!container.querySelector(copySelector))continue;
    const roots=current.filter(node=>container.contains(node));
    if(roots.length && roots.every(node=>current.indexOf(node)>=beforeAnswers))return roots.map(node=>node.innerText.trim()).join('\n\n');
  }
  return null;
}
function composer() {
  let rows=Array.from(document.querySelectorAll('#prompt-textarea')).filter(el=>el.offsetParent!==null);
  if(!rows.length)rows=Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[contenteditable="plaintext-only"]')).filter(el=>el.offsetParent!==null && !el.closest?.('[role="dialog"]'));
  if(rows.length!==1)throw new Error('没有找到唯一聊天输入框。请确认已登录；页面变化时需更新扩展。');
  return rows[0];
}
function submitButton(editor) {
  let rows=Array.from(document.querySelectorAll('button[data-testid="send-button"],button#composer-submit-button,button[aria-label="Send prompt"],button[aria-label="Send message"],button[aria-label="Send"],button[aria-label="发送提示"],button[aria-label="发送消息"],button[aria-label="发送"]'));
  if(!rows.length)rows=Array.from(editor.closest?.('form')?.querySelectorAll('button[type="submit"]')||[]);
  const unique=[...new Set(rows)].filter(el=>el.offsetParent!==null && el.getAttribute('data-testid')!=='stop-button');
  return unique.length===1&&!unique[0].disabled?unique[0]:null;
}
async function send(message) {
  if(busy)throw new Error('此网页已有桥接请求正在处理。');
  busy=true;let submitted=false;
  try {
    if(location.href!==message.payload.expected_url)throw new Error('聊天页已切换，未发送。');
    if(stopButton())throw new Error('网页正在生成其他答复，未发送。');
    const editor=composer();const beforeUsers=messages('user').length,beforeAnswers=messages('assistant').length;
    if(normalized(editor.value??editor.textContent))throw new Error('网页输入框有未发送草稿，未覆盖。请先在电脑处理。');
    const text=message.payload.text;
    if(editor instanceof HTMLTextAreaElement) {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(editor,text);
    } else if(editor.isContentEditable) {
      editor.focus();const range=document.createRange();range.selectNodeContents(editor);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      if(!document.execCommand('insertText',false,text))throw new Error('浏览器未接受后台文字输入，没有点击发送。');
    }else throw new Error('不支持的网页输入框。');
    editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));
    let button;
    for(let i=0;i<20;i++){button=submitButton(editor);if(button)break;await delay(250);}
    if(!button || normalized(editor.value??editor.textContent)!==normalized(text))throw new Error('输入或发送按钮未确认，没有点击发送；请检查网页草稿。');
    const active=await chrome.runtime.sendMessage({type:'active',id:message.id});if(!active?.active)throw new Error('本机请求已结束，未发送。');
    button.click();submitted=true;
    let observedUser=false,lastText='',stable=0;
    for(let i=0;i<1200;i++) {
      await delay(500);
      if(i%10===0){const state=await chrome.runtime.sendMessage({type:'active',id:message.id,progress:{phase:'awaiting_reply',users:messages('user').length,assistants:messages('assistant').length,articles:document.querySelectorAll('article').length,last_length:lastText.length,stop:!!stopButton()}});if(!state?.active)throw new Error('本机请求已结束；网页可能仍在生成。');}
      const users=messages('user');
      if(users.length>beforeUsers+1)throw new Error('网页同时出现其他输入，无法确认答复归属。');
      if(users.length===beforeUsers+1){if(normalized(users.at(-1).innerText)!==normalized(text))throw new Error('新消息与本次要求不一致。');observedUser=true;}
      const answers=messages('assistant');
      if(!observedUser||answers.length<=beforeAnswers)continue;
      const answer=answers.at(-1),value=answer.innerText.trim();
      if(value===lastText && value && !stopButton())stable++;else stable=0;
      lastText=value;
      const finished=completedText(answer,beforeAnswers);
      if(stable>=5 && finished)return {ok:true,text:finished.slice(0,24000),url:location.href};
    }
    throw new Error('未能确认网页答复完成，请检查网页。');
  }catch(error){return {ok:false,error:String(error.message||error)+(submitted?' 本次已点击发送，请勿重复提交。':'')};}
  finally{busy=false;}
}
function inspect(probe) {
  const describe=el=>({tag:el.tagName,id:el.id||'',class:el.className?.toString().slice(0,240)||'',role:el.getAttribute('role'),test:el.getAttribute('data-testid')});
  const chains=[];
  if(probe) {
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    while(walker.nextNode() && chains.length<6) {
      if(!walker.currentNode.textContent.includes(probe))continue;
      let element=walker.currentNode.parentElement;const chain=[];
      for(let i=0;i<9 && element && element!==document.body;i++,element=element.parentElement)chain.push(describe(element));
      chains.push(chain);
    }
  }
  const buttons=Array.from(document.querySelectorAll('button')).filter(el=>/copy|send|stop|复制|发送|停止/i.test((el.getAttribute('aria-label')||'')+' '+el.id+' '+el.getAttribute('data-testid'))).slice(-15).map(el=>({...describe(el),label:el.getAttribute('aria-label')}));
  return {ok:true,shape:{chains,buttons}};
}
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(message.type==='qq-ping'){reply({ok:true});return;}
  if(message.type==='qq-inspect'){reply(inspect(message.probe));return;}
  if(message.type==='qq-send'){send(message).then(reply);return true;}
});
