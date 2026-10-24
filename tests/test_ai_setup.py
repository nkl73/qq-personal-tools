from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_service import endpoint,list_models,models_endpoint
from ai_setup import PROVIDERS,credential_for,profile_from_form,provider_for,suggested_id
from tools import Catalog
from test_ai import KEY,MockAI


class AISetupTests(unittest.TestCase):
    def test_presets_fill_addresses_and_keep_models_user_selected(self):
        for provider in PROVIDERS[:-1]:
            profile=profile_from_form(provider,'my-selected-model')
            self.assertEqual(profile.base_url,provider.base_url)
            self.assertEqual(profile.model,'my-selected-model')
            self.assertEqual(profile.timeout,120)
            self.assertTrue(profile.name)
            self.assertEqual(provider_for(profile.base_url),provider)
            self.assertEqual(provider_for(endpoint(profile.base_url)),provider)
            with self.assertRaises(ValueError):profile_from_form(provider,'')
        self.assertFalse(PROVIDERS[0].can_list)

    def test_custom_service_and_advanced_values_are_optional(self):
        profile=profile_from_form(PROVIDERS[-1],'local-model','http://localhost:8080/v1',
            name='自选名称',prompt='固定要求',timeout='45',identifier='ai2')
        self.assertEqual(profile.id,'ai2');self.assertEqual(profile.timeout,45)
        self.assertEqual(profile.name,'自选名称');self.assertEqual(profile.system_prompt,'固定要求')
        self.assertEqual(provider_for(profile.base_url),PROVIDERS[-1])
        with self.assertRaises(ValueError):profile_from_form(PROVIDERS[-1],'model')

    def test_tool_identifiers_are_generated_without_overwriting_existing_ai(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog=Catalog(Path(folder)/'tools.json',Path(folder)/'shared')
            self.assertEqual(suggested_id(catalog),'ai')
            catalog.save_ai(profile_from_form(PROVIDERS[1],'fixture',identifier='ai'),'')
            self.assertEqual(suggested_id(catalog),'ai2')
            catalog.save_ai(profile_from_form(PROVIDERS[1],'fixture',identifier='ai2'),'')
            self.assertEqual(suggested_id(catalog),'ai3')

    def test_switching_platform_never_reuses_another_platforms_saved_key(self):
        old=profile_from_form(PROVIDERS[1],'fixture')
        self.assertEqual(credential_for(old,'',old,KEY),KEY)
        self.assertEqual(credential_for(replace(old,base_url=endpoint(old.base_url)),'',old,KEY),KEY)
        for provider in (PROVIDERS[0],PROVIDERS[2]):
            new=profile_from_form(provider,'fixture')
            with self.assertRaisesRegex(ValueError,'重新填写|自己的密钥|填写该平台'):
                credential_for(new,'',old,KEY)
            self.assertEqual(credential_for(new,'new-fixture-key',old,KEY),'new-fixture-key')

    def test_models_discovery_uses_get_and_removes_duplicates_or_secret_echoes(self):
        server=MockAI()
        try:
            result=list_models(server.url,KEY)
            self.assertEqual(result,['fixture-chat-a','fixture-chat-b'])
            path,auth,body=server.received[-1]
            self.assertEqual(path,'/v1/models');self.assertEqual(auth,'Bearer '+KEY)
            self.assertEqual(body,{'operation':'models'})
            self.assertEqual(models_endpoint(server.url+'/chat/completions'),server.url+'/models')
        finally:server.close()

    def test_unsupported_models_response_falls_back_to_manual_selection(self):
        server=MockAI();server.mode='malformed'
        try:
            with self.assertRaises(ValueError):list_models(server.url,KEY)
            self.assertEqual(len(server.received),1)
        finally:server.close()


if __name__=='__main__':unittest.main()
