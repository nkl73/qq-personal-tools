"""Sensible defaults for the short, provider-first AI setup form."""
from dataclasses import dataclass

from ai_service import AIProfile,endpoint,validate_key


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    can_list: bool
    hint: str


PROVIDERS=(
    Provider('火山方舟（按量 API）','https://ark.cn-beijing.volces.com/api/v3',False,
        '从火山控制台复制已开通的模型或接入点 ID。此选项用于按量 API，不使用 Coding Plan 套餐。'),
    Provider('DeepSeek','https://api.deepseek.com',True,'填好密钥后点“读取模型”，再从列表选择，也可直接填写。'),
    Provider('OpenAI','https://api.openai.com/v1',True,'填好 API 密钥后读取模型。请选支持文字对话的模型。'),
    Provider('其他兼容服务','',True,'仅此选项需填写服务地址；支持 Chat Completions 兼容文字接口。'),
)


def provider_for(base_url):
    for provider in PROVIDERS[:-1]:
        if endpoint(provider.base_url)==endpoint(base_url):return provider
    return PROVIDERS[-1]


def suggested_id(catalog):
    used={tool.id for tool in catalog.all()}
    value='ai';index=1
    while value in used:
        index+=1;value='ai'+str(index)
    return value


def profile_from_form(provider,model,base_url='',name='',prompt='',timeout=120,identifier='ai'):
    model=model.strip()
    base_url=(provider.base_url or base_url).strip()
    name=name.strip() or (provider.label.split('（')[0]+' · '+model)[:80]
    return AIProfile(identifier,name,base_url,model,prompt.strip(),int(timeout)).validate()


def credential_for(profile,typed,old_profile=None,old_key=''):
    typed=typed.strip()
    if typed:return validate_key(typed)
    if old_profile and old_key:
        if endpoint(old_profile.base_url)!=endpoint(profile.base_url):
            raise ValueError('平台或地址已更改，请填写该平台自己的密钥。')
        return old_key
    return ''
