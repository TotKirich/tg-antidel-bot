import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile


SPEC = importlib.util.spec_from_file_location('release_builder',Path(__file__).resolve().parents[1]/'scripts/Build-Release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.files=['README.md','config.example.json','start.sh','.gitignore','Dockerfile']
        for file in self.files:
            (self.root/file).write_text('{}' if file.endswith('.json') else 'safe source\n')
        self.original_files=release.FILES
        release.FILES=self.files
        self.addCleanup(setattr,release,'FILES',self.original_files)

    def test_public_folder_and_zip_exclude_runtime_and_history(self):
        (self.root/'.git').mkdir()
        (self.root/'.git/config').write_text('private-old-history')
        (self.root/'config.json').write_text(json.dumps(dict(api_hash='private-unique-value')))
        (self.root/'data').mkdir()
        (self.root/'data/messages.db').write_bytes(b'private messages')
        (self.root/'guard_mtproto.session').write_bytes(b'private session')
        destination=self.root/'publish/project'
        archive=release.build(self.root,destination)
        self.assertEqual({p.name for p in destination.iterdir()},set(self.files))
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(set(zipped.namelist()),{'tg-antidel-bot/'+p for p in self.files})
            self.assertIsNone(zipped.testzip())
        self.assertTrue(archive.with_name('tg-antidel-bot-github.zip').exists())
        with zipfile.ZipFile(archive.with_name('tg-antidel-bot-runtime.zip')) as zipped:
            self.assertFalse(any('/tests/' in name for name in zipped.namelist()))
            self.assertNotIn(b'FROM base AS test',zipped.read('tg-antidel-bot/Dockerfile'))

    def test_secret_in_example_or_selected_source_blocks_export(self):
        (self.root/'config.example.json').write_text(json.dumps(dict(bot_token='not-empty')))
        with self.assertRaises(ValueError): release.build(self.root)
        (self.root/'config.example.json').write_text('{}')
        (self.root/'config.json').write_text(json.dumps(dict(api_hash='private-unique-value')))
        (self.root/'README.md').write_text('leak: private-unique-value')
        with self.assertRaises(ValueError): release.build(self.root)
        self.assertFalse((self.root/'data/dist/tg-antidel-bot.zip').exists())

    def test_existing_publish_folder_is_never_overwritten(self):
        destination=self.root/'publish/project'
        destination.mkdir(parents=True)
        (destination/'keep').write_text('keep')
        with self.assertRaises(FileExistsError): release.build(self.root,destination)
        self.assertEqual((destination/'keep').read_text(),'keep')
