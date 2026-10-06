"""A80.1: memory evolution test and production paths close SQLite handles."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


_ROOT = Path(__file__).resolve().parents[1]
_TRACKER = r'''
import gc, io, sqlite3, traceback, unittest
original = sqlite3.connect
leaks = []
class Tracked(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_explicitly = False
        self.site = traceback.extract_stack(limit=8)
    def close(self):
        self.closed_explicitly = True
        return super().close()
    def __del__(self):
        if not self.closed_explicitly:
            leaks.append(next((f'{f.filename}:{f.lineno}' for f in reversed(self.site)
                if 'test_knowledge_evolution_v116.py' in f.filename), 'production'))
        try: super().close()
        except Exception: pass
def tracked_connect(*args, **kwargs):
    kwargs.setdefault('factory', Tracked)
    return original(*args, **kwargs)
sqlite3.connect = tracked_connect
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_knowledge_evolution_v116')
result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
gc.collect()
print('unclosed=' + str(len(leaks)))
for site in leaks: print(site)
raise SystemExit(0 if result.wasSuccessful() and not leaks else 1)
'''


class SQLiteConnectionHygieneTests(unittest.TestCase):
    def test_a80_paths_close_every_sqlite_connection(self):
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join((str(_ROOT / 'src'), str(_ROOT)))
        result = subprocess.run(
            [sys.executable, '-B', '-P', '-W', 'always::ResourceWarning',
             '-c', _TRACKER], cwd=_ROOT, env=env, capture_output=True, text=True,
            timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('unclosed=0', result.stdout)
