import io
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import config_ui


class ConfigUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'codex-home'
        self.home.mkdir()
        self.source = self.root / 'source.json'
        self.source.write_text(json.dumps({'models': [{'slug': 'gpt-6-astra', 'display_name': 'Astra'}]}))
        self.catalog = self.root / 'models.json'
        (self.home / 'config.toml').write_text('model = "gpt-6-astra"\n')
        self.settings = {
            'codex_home': str(self.home), 'source_catalog': str(self.source),
            'catalog_file': str(self.catalog),
            'routes': {'deepseek-flash': {
                'provider_name': 'DeepSeek', 'base_url': 'https://api.deepseek.com',
                'key_env': 'DEEPSEEK_API_KEY', 'efforts': ['low', 'high'], 'default_effort': 'high'}},
            'review': {'mode': 'terra', 'model': 'deepseek-flash', 'effort': 'high'},
            'subagent': {'model': 'deepseek-flash', 'effort': 'high'},
        }
        (self.root / 'settings.json').write_text(json.dumps(self.settings))
        self.store = config_ui.ConfigStore(self.root)

    def test_validation_rejects_unsafe_configuration(self):
        self.assertEqual(config_ui.validate_base_url('https://example.com/v1/'), 'https://example.com/v1')
        self.assertEqual(config_ui.validate_base_url('http://127.0.0.1:8080/v1'), 'http://127.0.0.1:8080/v1')
        for url in ('http://example.com/v1', 'https://user:pass@example.com/v1', 'https://example.com/v1?q=x'):
            with self.assertRaises(ValueError):
                config_ui.validate_base_url(url)
        with self.assertRaises(ValueError):
            config_ui.validate_model_id('../bad model')
        with self.assertRaises(ValueError):
            config_ui.validate_env_name('bad-name')

    def test_model_discovery_reads_standard_response_and_deduplicates(self):
        payload = json.dumps({'data': [{'id': 'model-a'}, {'id': 'model-a'}, {'id': 'org/model-b'}]}).encode()
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return io.BytesIO(payload)
        opener = Opener()
        with patch.object(config_ui, 'build_opener', return_value=opener):
            result = config_ui.fetch_model_ids('https://gateway.example/v1', 'test-secret')
        self.assertEqual(result, ['model-a', 'org/model-b'])
        self.assertEqual(opener.request.full_url, 'https://gateway.example/v1/models')

    def test_state_redacts_secret_and_lists_primary_and_external_models(self):
        with patch.object(config_ui, 'get_user_secret', return_value='hidden-secret'):
            state = self.store.state()
        self.assertEqual(state['current_model'], 'gpt-6-astra')
        self.assertEqual(state['primary_models'], [{'id': 'gpt-6-astra', 'name': 'Astra'}])
        self.assertTrue(state['routes'][0]['key_configured'])
        self.assertNotIn('api_key', state['routes'][0])
        self.assertNotIn('hidden-secret', json.dumps(state))

    def test_save_route_preferences_and_primary_model(self):
        secret = {'value': ''}
        def save_secret(name, value):
            secret['value'] = value
        with patch.object(config_ui, 'set_user_secret', side_effect=save_secret), \
             patch.object(config_ui, 'get_user_secret', side_effect=lambda name: secret['value']):
            self.store.save_route({
                'model_id': 'vendor/model-x', 'provider_name': 'Vendor',
                'base_url': 'https://vendor.example/v1', 'key_env': 'VENDOR_API_KEY',
                'api_key': 'never-store-this', 'efforts': ['medium', 'high'],
                'default_effort': 'medium'})
        saved = self.store.load_settings()
        self.assertIn('vendor/model-x', saved['routes'])
        self.assertNotIn('never-store-this', self.store.settings_path.read_text())
        self.store.save_preferences({
            'review_mode': 'external', 'review_model': 'vendor/model-x', 'review_effort': 'high',
            'subagent_model': 'vendor/model-x', 'subagent_effort': 'medium'})
        self.store.save_primary_model({'model': 'vendor/model-x'})
        self.assertEqual(self.store.load_settings()['review']['model'], 'vendor/model-x')
        self.assertEqual(tomllib.loads((self.home / 'config.toml').read_text())['model'], 'vendor/model-x')

    def test_renaming_route_updates_active_references(self):
        (self.home / 'config.toml').write_text('model = "deepseek-flash"\n')
        with patch.object(config_ui, 'get_user_secret', return_value='existing-secret'):
            self.store.save_route({
                'original_model_id': 'deepseek-flash', 'model_id': 'deepseek-chat',
                'provider_name': 'DeepSeek', 'base_url': 'https://api.deepseek.com',
                'key_env': 'DEEPSEEK_API_KEY', 'api_key': '',
                'efforts': ['low', 'high'], 'default_effort': 'high'})
        saved = self.store.load_settings()
        self.assertNotIn('deepseek-flash', saved['routes'])
        self.assertEqual(saved['review']['model'], 'deepseek-chat')
        self.assertEqual(saved['subagent']['model'], 'deepseek-chat')
        self.assertEqual(tomllib.loads((self.home / 'config.toml').read_text())['model'], 'deepseek-chat')


if __name__ == '__main__':
    unittest.main()
