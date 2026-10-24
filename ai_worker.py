"""One bounded model request. Tool execution stays in the local task engine."""
import json
import re
import socket
import sys
from urllib.error import HTTPError,URLError
from urllib.request import build_opener,HTTPRedirectHandler,Request

from ai_service import AIProfile,MAX_REPLY,endpoint,models_endpoint,validate_key


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def request(profile,key,messages,operation='chat',tools=None):
    profile.validate();validate_key(key)
    payload={'model':profile.model,'messages':messages,'stream':False}
    if operation=='agent':
        if not profile.allow_tools or not isinstance(tools,list) or not 1<=len(tools)<=20:raise ValueError('电脑工具配置无效。')
        payload.update(tools=tools,tool_choice='auto')
    body=json.dumps(payload,ensure_ascii=False).encode('utf-8')
    headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'QQ-Personal-Tools/0.5'}
    if key:headers['Authorization']='Bearer '+key
    req=Request(models_endpoint(profile.base_url) if operation=='models' else endpoint(profile.base_url),
        data=None if operation=='models' else body,headers=headers,method='GET' if operation=='models' else 'POST')
    try:
        with build_opener(NoRedirect()).open(req,timeout=profile.timeout) as response:
            data=response.read(2*1024*1024+1)
        if len(data)>2*1024*1024:raise ValueError('AI 返回内容超过 2 MB，未接收。请缩小问题范围。')
        value=json.loads(data)
        if operation=='models':
            rows=value['data']
            if not isinstance(rows,list):raise ValueError('此服务未提供可读取的模型列表，请直接填写模型名称。')
            models=[]
            for row in rows[:2000]:
                identifier=row.get('id') if isinstance(row,dict) else None
                if (isinstance(identifier,str) and 0<len(identifier)<=200 and not any(ord(c)<32 for c in identifier)
                    and not (key and key in identifier) and identifier not in models):models.append(identifier)
                if len(models)>=500:break
            if not models:raise ValueError('模型列表为空，请在平台开通模型后重试，或直接填写模型名称。')
            return models
        choice=value['choices'][0]
        message=choice['message']
        content=message.get('content') or message.get('refusal')
        if isinstance(content,list):content=''.join(p.get('text','') for p in content if isinstance(p,dict) and p.get('type')=='text')
        calls=message.get('tool_calls')
        if operation=='agent' and calls:
            if choice.get('finish_reason')=='length':raise ValueError('模型工具参数被截断，未执行。请缩小要求。')
            if not isinstance(calls,list) or len(calls)>8:raise ValueError('模型工具调用格式或数量无效，未执行。')
            normalized=[];ids=set()
            for item in calls:
                function=item.get('function',{}) if isinstance(item,dict) else {}
                uid=item.get('id') if isinstance(item,dict) else None
                name=function.get('name');arguments=function.get('arguments')
                if (not isinstance(uid,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',uid) or uid in ids
                    or item.get('type')!='function' or not isinstance(name,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',name)
                    or not isinstance(arguments,str) or len(arguments)>24000):raise ValueError('模型工具调用格式无效，未执行。')
                ids.add(uid)
                normalized.append({'id':uid,'type':'function','function':{'name':name,'arguments':arguments.replace(key,'[密钥已隐藏]') if key else arguments}})
            content=content.replace(key,'[密钥已隐藏]') if key and isinstance(content,str) else content
            result={'role':'assistant','content':(content[:MAX_REPLY] if isinstance(content,str) else None),'tool_calls':normalized}
            reasoning=message.get('reasoning_content')
            if isinstance(reasoning,str) and len(reasoning)<=48000:result['reasoning_content']=reasoning.replace(key,'[密钥已隐藏]') if key else reasoning
            return result
        if not isinstance(content,str) or not content.strip():
            if message.get('tool_calls'):raise ValueError('此模型要求调用工具；当前 AI 入口仅支持文字对话。')
            raise ValueError('AI 未返回文字，请检查模型是否支持 Chat Completions 文本接口。')
        if len(content)>MAX_REPLY:content=content[:MAX_REPLY]+'\n[回复过长，已截断；可要求分段回答。]'
        if choice.get('finish_reason')=='length':content+='\n[服务端达到输出上限，可要求继续。]'
        content=content.replace(key,'[密钥已隐藏]') if key else content
        return {'role':'assistant','content':content} if operation=='agent' else content
    except HTTPError as error:
        # Never surface arbitrary provider error bodies: they may echo credentials.
        hints={401:'密钥无效或已失效，请在电脑检查 API Key。',403:'服务拒绝访问，请检查账号和模型权限。',
            404:'接口地址或模型不存在，请在电脑检查地址和模型 ID。',429:'调用过于频繁或额度不足，请在服务商处检查。'}
        message=hints.get(error.code,'服务暂时不可用，请检查服务商状态。')
        if error.code==400 and operation=='agent':message='请检查模型是否支持工具调用；可关闭“允许 AI 使用电脑工具”后使用文字对话。'
        if 300<=error.code<400:message='服务返回重定向，请直接填写最终 HTTPS 接口地址。'
        raise ValueError('AI 接口返回 HTTP %d：%s 不会自动重发。'%(error.code,message)) from None
    except (socket.timeout,TimeoutError):raise ValueError('AI 请求超时，不会自动重发。') from None
    except URLError:raise ValueError('无法连接 AI 服务，请检查网络和服务地址；不会自动重发。') from None
    except (KeyError,IndexError,TypeError,json.JSONDecodeError,UnicodeError):
        raise ValueError('返回内容不符合 Chat Completions 文本格式，请检查接口类型。') from None


def main():
    try:
        raw=sys.stdin.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:raise ValueError('请求过长。')
        data=json.loads(raw)
        operation=data.get('operation','chat')
        if operation not in ('chat','models','agent'):raise ValueError('不支持此 AI 操作。')
        content=request(AIProfile(**data['profile']),data['key'],data['messages'],operation,data.get('tools'))
        result={'ok':True,{'models':'models','agent':'message'}.get(operation,'text'):content}
    except ValueError as error:result={'ok':False,'error':str(error)}
    except Exception:result={'ok':False,'error':'AI 请求失败，请检查接口设置和网络。'}
    sys.stdout.write(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
