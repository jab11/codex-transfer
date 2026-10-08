import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from test_transfer import initialize, add_project, add_thread, db, read_db
import transfer as t
spec = importlib.util.spec_from_file_location('remap',ROOT/'remap-codex-paths.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RemapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base/'home'
        self.old = self.base/'old'/'Project'
        self.new = self.base/'new'/'Project'
        (self.new/'nested').mkdir(parents=True)
        self.source = self.new/'app.txt'
        self.source.write_text('source stays unchanged')
        self.source_hash = t.hash_file(self.source)
        initialize(self.home)
        add_project(self.home,'project','Project',self.old)
        self.session = add_thread(self.home,'a',self.old/'nested',text=str(self.old))
        add_thread(self.home,'b',self.base/'elsewhere')
        self.before_hashes = {str(p):t.hash_file(p) for p in self.home.rglob('*') if p.is_file()}
        self.global_state = {
            'local-projects':{'project':{'rootPaths':[str(self.old)],'name':'Project'}},
            'thread-workspace-root-hints':{'a':[str(self.old)]},
            'thread-writable-roots':{'a':[str(self.old/'nested')]},
            'thread-titles':{'titles':{'a':'Keep history '+str(self.old)}},
            'queued-follow-ups':{'a':str(self.old)},
            'electron-persisted-atom-state':{
                'thread-workspace-state-v1:a':{'applied':{'cwd':str(self.old/'nested')}},
                'remote-thread-summaries-v3:other':{'cwd':str(self.old)}},
            'remote-projects':{'other':{'rootPaths':[str(self.old)]}}
        }
        t.dump(self.home/t.GLOBAL,self.global_state)
        self.before_hashes[str(self.home/t.GLOBAL)] = t.hash_file(self.home/t.GLOBAL)
        self.maps = t.path_maps([str(self.old)+'='+str(self.new)])

    def tearDown(self):
        self.assertEqual(t.hash_file(self.source),self.source_hash)
        self.tmp.cleanup()

    def test_preview_changes_nothing(self):
        result = m.repair(self.home,self.maps)
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['updated_threads'],1)
        self.assertEqual(result['project_roots'][0]['new'],str(self.new))
        self.assertFalse((self.home/'transfer-backups').exists())
        for p,h in self.before_hashes.items():
            self.assertEqual(t.hash_file(Path(p)),h)

    def test_apply_changes_only_live_path_metadata(self):
        with patch.object(t,'ensure_codex_stopped') as guard:
            result = m.repair(self.home,self.maps,apply=True)
        self.assertEqual(guard.call_count,2)
        self.assertEqual(result['status'],'complete')
        self.assertTrue((Path(result['backup'])/'receipt.json').exists())
        with read_db(self.home/t.STATE) as c:
            self.assertEqual(c.execute("SELECT cwd FROM threads WHERE id='a'").fetchone()[0],str(self.new/'nested'))
            self.assertEqual(c.execute('SELECT path FROM project_roots').fetchone()[0],str(self.new))
            self.assertEqual(c.execute('SELECT count(*) FROM threads').fetchone()[0],2)
        g = t.read_json(self.home/t.GLOBAL)
        self.assertEqual(g['local-projects']['project']['rootPaths'],[str(self.new)])
        self.assertEqual(g['thread-writable-roots']['a'],[str(self.new/'nested')])
        self.assertEqual(g['electron-persisted-atom-state']['thread-workspace-state-v1:a']['applied']['cwd'],str(self.new/'nested'))
        for key in ('queued-follow-ups','thread-titles','remote-projects'):
            self.assertEqual(g[key],self.global_state[key])
        self.assertEqual(g['electron-persisted-atom-state']['remote-thread-summaries-v3:other'],self.global_state['electron-persisted-atom-state']['remote-thread-summaries-v3:other'])
        self.assertEqual(t.hash_file(self.session),self.before_hashes[str(self.session)])
        self.assertEqual(t.hash_file(self.home/t.HISTORY),self.before_hashes[str(self.home/t.HISTORY)])
        again = m.repair(self.home,self.maps)
        self.assertEqual(again['updated_threads'],0)
        self.assertEqual(again['project_roots'],[])

    def test_missing_target_rejected(self):
        maps = t.path_maps([str(self.old)+'='+str(self.base/'missing')])
        with self.assertRaisesRegex(t.TransferError,'does not exist'):
            m.repair(self.home,maps,apply=True)

    def test_duplicate_project_roots_rejected(self):
        add_project(self.home,'other','Other',self.new)
        with self.assertRaisesRegex(t.TransferError,'duplicate project roots'):
            m.repair(self.home,self.maps)

    def test_install_failure_restores_originals(self):
        replace = m.os.replace
        def broken_replace(src,dest):
            if Path(dest) == self.home/t.GLOBAL:
                raise OSError('simulated global install failure')
            return replace(src,dest)
        with patch.object(t,'ensure_codex_stopped'),patch.object(m.os,'replace',side_effect=broken_replace):
            with self.assertRaisesRegex(OSError,'simulated'):
                m.repair(self.home,self.maps,apply=True)
        with read_db(self.home/t.STATE) as c:
            self.assertEqual(c.execute("SELECT cwd FROM threads WHERE id='a'").fetchone()[0],str(self.old/'nested'))
        self.assertEqual(t.read_json(self.home/t.GLOBAL),self.global_state)
        self.assertEqual(t.hash_file(self.session),self.before_hashes[str(self.session)])
        self.assertEqual(t.hash_file(self.home/t.HISTORY),self.before_hashes[str(self.home/t.HISTORY)])

    def test_reopened_client_blocks_installation(self):
        with patch.object(t,'ensure_codex_stopped',side_effect=[None,t.TransferError('client reopened')]):
            with self.assertRaisesRegex(t.TransferError,'client reopened'):
                m.repair(self.home,self.maps,apply=True)
        for p,h in self.before_hashes.items():
            self.assertEqual(t.hash_file(Path(p)),h)


if __name__ == '__main__':
    unittest.main()
