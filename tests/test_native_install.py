import sys
import tomllib
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_install import render_config, render_rules

BASE = '''model = "gpt-6-astra"
model_provider = "primary"
model_catalog_json = "old.json"
[model_providers.primary]
name = "existing"
base_url = "https://gateway.example.test/v1"
requires_openai_auth = true
supports_websockets = true
[model_providers.other]
base_url = "https://example.invalid"
[features]
some_feature = true
'''

class InstallRenderingTests(unittest.TestCase):
    def test_provider_settings_stay_in_right_table(self):
        result = tomllib.loads(render_config(BASE, 'primary', {'port': 12345, 'catalog_file': 'C:\\path\\models.json'}, 'test-not-real-secret'))
        provider = result['model_providers']['primary']
        self.assertEqual(provider['base_url'], 'http://127.0.0.1:12345')
        self.assertEqual(provider['http_headers']['X-Native-Adapter-Token'], 'test-not-real-secret')
        self.assertEqual(provider['request_max_retries'], 0)
        self.assertEqual(provider['stream_max_retries'], 0)
        self.assertIs(provider['supports_websockets'], False)
        self.assertIs(provider['requires_openai_auth'], True)
        self.assertEqual(result['model_provider'], 'primary')
        self.assertEqual(result['model_providers']['other']['base_url'], 'https://example.invalid')
        self.assertEqual(result['features'], {'some_feature': True})

    def test_existing_auth_headers_require_explicit_merge(self):
        text = BASE.replace('name = "existing"', 'name = "existing"\nhttp_headers = { "Other" = "x" }')
        with self.assertRaises(ValueError): render_config(text, 'primary', {'port': 12345, 'catalog_file': 'new'}, 'test')

    def test_rules_preserve_unrelated_sections(self):
        original = '# Global\n\n## Models\nKeep models.\n\n## DeepSeek Delegation (Old)\nOld rule.\n\n## Skill Selection\nKeep skills.\n'
        result = render_rules(original)
        self.assertTrue(result.startswith('# Global\n\n## Models\nKeep models.'))
        self.assertTrue(result.endswith('## Skill Selection\nKeep skills.\n'))
        self.assertNotIn('Old rule.', result)
        self.assertEqual(result.count('## External Model Delegation'), 1)
        self.assertNotIn('## DeepSeek Delegation', result)
        self.assertEqual(render_rules(result), result)

if __name__ == '__main__': unittest.main()
