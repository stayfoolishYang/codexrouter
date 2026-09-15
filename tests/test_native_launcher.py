import hashlib
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import native_launcher
from test_native_install import BASE


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(native_launcher, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.path = self.root / 'config.toml'
        self.path.write_text(BASE)
        (self.root / 'adapter-token.txt').write_text('test-token')
        self.settings = dict(codex_home=str(self.root), port=12345,
                             original_provider='primary', parent_base_url='https://gateway.example.test/v1',
                             token_sha256=hashlib.sha256(b'test-token').hexdigest(), catalog_file=str(self.root / 'models.json'))

    def test_restore_then_idempotent(self):
        self.assertTrue(native_launcher.reconcile_config(self.settings))
        first = self.path.read_bytes()
        self.assertFalse(native_launcher.reconcile_config(self.settings))
        self.assertEqual(first, self.path.read_bytes())
        config = tomllib.loads(first.decode())
        self.assertEqual(config['model'], 'gpt-6-astra')
        self.assertEqual(config['model_providers']['other']['base_url'], 'https://example.invalid')
        self.assertEqual(len(list((self.root / 'backups').iterdir())), 1)

    def test_unrelated_route_preserved(self):
        self.path.write_text(BASE.replace('gateway.example.test', 'other.example.test'))
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError): native_launcher.reconcile_config(self.settings)
        self.assertEqual(before, self.path.read_bytes())

    def test_removed_provider_preserved(self):
        self.path.write_text('model = "gpt-6-astra"\n')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'provider was removed'):
            native_launcher.reconcile_config(self.settings)
        self.assertEqual(before, self.path.read_bytes())

    def test_custom_headers_preserved(self):
        self.path.write_text(BASE.replace('name = "existing"', 'name = "existing"\nhttp_headers = { "Other" = "value" }'))
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError): native_launcher.reconcile_config(self.settings)
        self.assertEqual(before, self.path.read_bytes())

    def test_token_mismatch_preserved(self):
        (self.root / 'adapter-token.txt').write_text('wrong')
        with self.assertRaises(RuntimeError): native_launcher.reconcile_config(self.settings)
        self.assertEqual(self.path.read_text(), BASE)

    def test_existing_ds_catalog_gets_new_effort(self):
        source = self.root / 'source.json'
        source.write_text(json.dumps({'models': [{'slug': 'gpt-5.5'}, {'slug': 'deepseek-flash', 'default_reasoning_level': 'medium'}]}))
        self.settings.update(source_catalog=str(source), routes={'deepseek-flash': {'default_effort': 'high', 'efforts': ['low', 'medium', 'high', 'xhigh']}})
        native_launcher.catalog(self.settings)
        models = json.loads(Path(self.settings['catalog_file']).read_text())['models']
        children = [m for m in models if m['slug'] == 'deepseek-flash']
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['default_reasoning_level'], 'high')
        source.unlink()
        native_launcher.catalog(self.settings)
        cached = json.loads(Path(self.settings['catalog_file']).read_text())['models']
        self.assertEqual(len([m for m in cached if m['slug'] == 'deepseek-flash']), 1)


if __name__ == '__main__':
    unittest.main()
