import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy


class DeployComparisonTests(unittest.TestCase):
    def test_snapshot_comparison_and_cleanup(self):
        batch = {r: ('unused', c) for r, c in
                 [('new.ts', 'new'), ('lib/same.ts', 'same'), ('updated.ts', 'new')]}
        directories = []
        real_tempdir = tempfile.TemporaryDirectory

        def tempdir(**kwargs):
            directory = real_tempdir(**kwargs)
            directories.append(directory.name)
            return directory

        def get(method, url):
            self.assertEqual(method, 'GET')
            return 200, b'same' if 'same.ts' in url else b'old'

        with patch.object(deploy, 'call', side_effect=get), patch.object(
                deploy.tempfile, 'TemporaryDirectory', side_effect=tempdir):
            states = deploy.compare_remote(batch, {'lib/same.ts', 'updated.ts'}, 2)
        self.assertEqual(states, {'new.ts': 'NEW', 'lib/same.ts': 'SAME', 'updated.ts': 'UPDATED'})
        self.assertTrue(directories)
        self.assertFalse(os.path.exists(directories[0]))

    def run_deploy(self, args, remote, fail_download=False):
        calls = []

        def api(method, url, body=None):
            calls.append((method, url))
            if method == 'GET' and '/content?' in url:
                if fail_download:
                    return 503, b'unavailable'
                name = url.split('path=')[1]
                return 200, remote[name].encode()
            if method == 'PUT':
                return 200, b''
            if method == 'DELETE':
                return 204, b''
            self.fail('Unexpected API request: ' + method + ' ' + url)

        with patch.dict(os.environ, {'VT': 'test'}), patch('sys.argv', ['deploy.py'] + args), \
                patch.object(deploy, 'list_remote', return_value=list(remote)), \
                patch.object(deploy, 'call', side_effect=api), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = deploy.main()
        return result, calls

    def test_upload_only_changes_and_keep_unchanged_in_prune_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            cwd = os.getcwd()
            stack.callback(os.chdir, cwd)
            os.chdir(directory)
            for name, content in [('same.ts', 'same'), ('updated.ts', 'new'), ('new.ts', 'new')]:
                Path(name).write_text(content)
            result, calls = self.run_deploy(['same.ts', 'updated.ts', 'new.ts', '--prune', '-y'],
                                            {'same.ts': 'same', 'updated.ts': 'old', 'gone.ts': 'old'})
            self.assertEqual(result, 0)
            writes = [(m, u.split('path=')[1]) for m, u in calls if m != 'GET']
            self.assertEqual(sorted(writes), [('DELETE', 'gone.ts&recursive=false'),
                                             ('PUT', 'new.ts'), ('PUT', 'updated.ts')])
            self.assertTrue(all(m == 'GET' for m, _ in calls[:2]))
            state = json.loads(Path(deploy.DEPLOY_MANIFEST).read_text())
            self.assertEqual(set(state['files']), {'same.ts', 'updated.ts', 'new.ts'})

    def test_failed_snapshot_and_dry_run_never_write(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            cwd = os.getcwd()
            stack.callback(os.chdir, cwd)
            os.chdir(directory)
            Path('app.ts').write_text('new')
            for args, fail, expected in [(['app.ts', '--prune', '-y'], True, 1),
                                         (['app.ts', '--dry-run', '--prune'], False, 0)]:
                result, calls = self.run_deploy(args, {'app.ts': 'old'}, fail)
                self.assertEqual(result, expected)
                self.assertTrue(all(m == 'GET' for m, _ in calls))
                self.assertFalse(Path(deploy.DEPLOY_MANIFEST).exists())

    def test_whitespace_changes_are_not_skipped(self):
        with patch.object(deploy, 'call', return_value=(200, b'hello\n')):
            self.assertEqual(deploy.compare_remote({'a.ts': ('unused', 'hello')}, {'a.ts'}, 1),
                             {'a.ts': 'UPDATED'})


if __name__ == '__main__':
    unittest.main()
