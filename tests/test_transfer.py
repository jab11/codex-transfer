"""Integration tests against the inspected SQLite schemas, using invented data only."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import transfer as t


def call(script, *args, json_output=True):
    flags = ['--json'] if json_output else []
    return subprocess.run([sys.executable, str(ROOT/script), *map(str, args), *flags], text=True, capture_output=True)


@contextlib.contextmanager
def db(path):
    with contextlib.closing(sqlite3.connect(path)) as c:
        with c:
            yield c


def read_db(path):
    return contextlib.closing(t.ro(path))


def add_obsolete_sequence(path):
    with db(path) as c:
        c.execute('CREATE TABLE obsolete (id INTEGER PRIMARY KEY AUTOINCREMENT)')
        c.execute('INSERT INTO obsolete DEFAULT VALUES')
        c.execute('DROP TABLE obsolete')
        c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('retired_history',42)")


def initialize(home):
    home.mkdir(parents=True, exist_ok=True)
    schemas = json.loads((ROOT/'tests/schema.json').read_text())
    for name, statements in schemas.items():
        with db(home/name) as c:
            for sql in statements:
                c.execute(sql)
            c.execute("INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES(1,'test-schema',1,?,0)", (b'test',))


def add_project(home, pid, name, root):
    with db(home/t.STATE) as c:
        c.execute("INSERT INTO projects VALUES(?,?,?,0,1000,1000)", (pid,name,'{}'))
        c.execute("INSERT INTO project_roots VALUES(?,0,?)", (pid,str(root)))


def add_thread(home, tid, cwd, text='Hello', parent=None, fork=None, sandbox='workspace-write'):
    path = home/'sessions'/'2026'/'10'/('rollout-2026-10-08-'+tid+'.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {'id':tid, 'session_id':tid, 'cwd':str(cwd), 'history_mode':'paginated'}
    if fork:
        meta['forked_from_id'] = fork
        meta['session_id'] = fork
    item = {'id':'item-'+tid,'type':'userMessage','content':[{'type':'text','text':text}]}
    line1 = json.dumps({'type':'session_meta','payload':meta})+'\n'
    line2 = json.dumps({'type':'event_msg','payload':item})+'\n'
    path.write_text(line1+line2)
    with db(home/t.STATE) as c:
        c.execute("INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,sandbox_policy,approval_mode,history_mode,memory_mode,daybreak_enabled) VALUES(?,?,1,1,'vscode','openai',?,?,?,'never','paginated','enabled',1)",
                  (tid,str(path),str(cwd),'Thread '+tid,json.dumps({'type':sandbox})))
        if parent:
            c.execute("INSERT INTO thread_spawn_edges VALUES(?,?,'completed')", (parent,tid))
    with db(home/t.HISTORY) as c:
        c.execute("INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,rollout_byte_offset,rollout_end_byte_offset) VALUES(?, ?,1,'completed',?,?)", (tid,'turn-'+tid,len(line1.encode()),path.stat().st_size))
        c.execute("INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type) VALUES(?,?,?,1,1000,?,'userMessage')",(tid,'turn-'+tid,'item-'+tid,json.dumps(item)))
        c.execute("INSERT INTO thread_history_projection_state VALUES(?,?,2)",(tid,path.stat().st_size))
    return path


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base/'source-home'
        self.dest = self.base/'dest-home'
        self.bundle = self.base/'bundle'
        initialize(self.source)
        self.alpha = self.base/'code'/'alpha'
        self.beta = self.alpha/'nested-beta'
        self.alpha.mkdir(parents=True)
        (self.alpha/'app.py').write_text('source code must remain unchanged\n')
        self.code_hash = t.hash_file(self.alpha/'app.py')
        add_project(self.source,'native-alpha','Alpha',self.alpha)
        add_project(self.source,'native-beta','Beta',self.beta)
        self.a_path = add_thread(self.source,'a',self.alpha)
        add_thread(self.source,'b',self.beta,'private beta text')
        add_thread(self.source,'c',self.base/'worktree',parent='a')
        self.originals = {p.relative_to(self.source).as_posix():t.hash_file(p) for p in self.source.rglob('*') if p.is_file()}
        g = {'local-projects':{'legacy-alpha':{'id':'legacy-alpha','name':'Alpha','rootPaths':[str(self.alpha)]}},
             'app-server-project-id-by-legacy-project-id-by-host':{'local:'+str(self.source):{'legacy-alpha':'native-alpha'}},
             'thread-project-assignments':{'a':{'projectKind':'local','projectId':'legacy-alpha'}},
             'thread-titles':{'titles':{'a':'A title','b':'B title'},'order':['a','b']},
             'queued-follow-ups':{'a':'must not copy'},'electron-local-remote-control-installation-id':'secret-device',
             'thread-writable-roots':{'a':[str(self.alpha)]}}
        t.dump(self.source/t.GLOBAL,g)
        self.originals[t.GLOBAL] = t.hash_file(self.source/t.GLOBAL)

    def tearDown(self):
        self.assertEqual(t.hash_file(self.alpha/'app.py'),self.code_hash)
        self.tmp.cleanup()

    def export(self, *selectors):
        r = call('codex-export.py','--codex-home',self.source,*selectors,'--output',self.bundle,'--codex-stopped')
        self.assertEqual(r.returncode,0,r.stderr)
        return t.verify_bundle(self.bundle)

    def import_bundle(self, *flags):
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--codex-stopped',*flags)
        self.assertEqual(r.returncode,0,r.stderr)
        return json.loads(r.stdout)

    def test_project_filter_dependencies_and_secrets(self):
        m = self.export('--project','Alpha')
        self.assertEqual(set(m['sessions']),{'a','c'})
        self.assertEqual(m['dependency_ids'],['c'])
        with read_db(self.bundle/t.STATE) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM projects').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM remote_control_enrollments').fetchone()[0],0)
        content = (self.bundle/'global-state.json').read_text()
        self.assertNotIn('secret-device',content)
        self.assertNotIn('queued-follow-ups',content)
        self.assertNotIn('B title',content)
        for p in self.source.rglob('*'):
            if p.is_file() and p.relative_to(self.source).as_posix() in self.originals:
                self.assertEqual(t.hash_file(p),self.originals[p.relative_to(self.source).as_posix()])

    def test_fork_includes_parent_without_other_project(self):
        add_thread(self.source,'fork',self.alpha,fork='a')
        m = self.export('--thread','fork')
        self.assertEqual(set(m['sessions']),{'a','fork'})

    def test_restore_archived_and_path_mapping_with_existing_data(self):
        initialize(self.dest)
        add_project(self.dest,'destination-alpha','Alpha existing',self.base/'new-code')
        add_thread(self.dest,'existing',self.base/'new-code','keep this unchanged')
        with read_db(self.dest/t.STATE) as c:
            before = dict(c.execute("SELECT * FROM threads WHERE id='existing'").fetchone())
        t.dump(self.dest/t.GLOBAL,{'queued-follow-ups':{'existing':'preserve'},'electron-local-remote-control-installation-id':'destination-device'})
        m = self.export('--project','Alpha')
        report = self.import_bundle('--status','archived','--path-map',str(self.alpha)+'='+str(self.base/'new-code'))
        with read_db(self.dest/t.STATE) as c:
            self.assertEqual(dict(c.execute("SELECT * FROM threads WHERE id='existing'").fetchone()),before)
            for row in c.execute("SELECT * FROM threads WHERE id IN ('a','c')"):
                self.assertEqual(row['archived'],1)
                self.assertEqual(json.loads(row['sandbox_policy']),{'type':'read-only'})
                self.assertEqual(row['memory_mode'],'disabled')
                self.assertEqual(row['daybreak_enabled'],0)
                self.assertEqual(t.hash_file(Path(row['rollout_path'])),m['files'][m['sessions'][row['id']]]['sha256'])
            a = c.execute("SELECT * FROM threads WHERE id='a'").fetchone()
            self.assertEqual(a['cwd'],str(self.base/'new-code'))
            self.assertEqual(a['project_id'],'destination-alpha')
        self.assertTrue((Path(report['backup'])/'receipt.json').exists())
        g = t.read_json(self.dest/t.GLOBAL)
        self.assertEqual(g['electron-local-remote-control-installation-id'],'destination-device')
        self.assertEqual(g['queued-follow-ups'],{'existing':'preserve'})

    def test_large_thread_and_binary_asset(self):
        large = 'x'*(12*1024*1024)
        add_thread(self.source,'large',self.alpha,large)
        asset = self.source/'attachments'/'picture.bin'
        asset.parent.mkdir()
        asset.write_bytes(bytes(range(256))*50)
        with db(self.source/t.HISTORY) as c:
            item = {'type':'localImage','path':str(asset),'text':'keep '+str(asset)}
            c.execute("UPDATE thread_items SET item_json=? WHERE thread_id='a'",(json.dumps(item),))
        m = self.export('--thread','large','--thread','a')
        self.assertGreater(m['files'][m['sessions']['large']]['bytes'],10*1024*1024)
        self.import_bundle()
        with read_db(self.dest/t.HISTORY) as c:
            item = json.loads(c.execute("SELECT item_json FROM thread_items WHERE thread_id='a'").fetchone()[0])
            self.assertEqual(t.hash_file(Path(item['path'])),t.hash_file(asset))
            self.assertEqual(item['text'],'keep '+str(asset))
            self.assertEqual(len(json.loads(c.execute("SELECT item_json FROM thread_items WHERE thread_id='large'").fetchone()[0])['content'][0]['text']),len(large))

    def test_referenced_visualization_directory(self):
        directory = self.source/'visualizations'/'a'
        directory.mkdir(parents=True)
        (directory/'index.html').write_text('<p>test chart</p>')
        with db(self.source/t.HISTORY) as c:
            c.execute("UPDATE thread_items SET item_json=? WHERE thread_id='a'",(json.dumps({'path':str(directory)}),))
        m = self.export('--thread','a')
        self.assertEqual(m['missing_assets'],[])
        self.assertIn(str(directory),m['asset_directories'])
        self.import_bundle()
        with read_db(self.dest/t.HISTORY) as c:
            item = json.loads(c.execute("SELECT item_json FROM thread_items WHERE thread_id='a'").fetchone()[0])
            self.assertEqual((Path(item['path'])/'index.html').read_text(),'<p>test chart</p>')

    def test_duplicate_import_never_overwrites(self):
        self.export('--thread','a')
        self.import_bundle()
        before = t.hash_file(self.dest/t.STATE)
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--codex-stopped')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('already exist',r.stderr)
        self.assertEqual(t.hash_file(self.dest/t.STATE),before)
        r = self.import_bundle('--skip-existing')
        self.assertEqual(r['import_threads'],0)
        self.assertEqual(t.hash_file(self.dest/t.STATE),before)

    def test_preserve_active_and_archived_status(self):
        with db(self.source/t.STATE) as c:
            c.execute("UPDATE threads SET archived=1,archived_at=42 WHERE id='b'")
        self.export('--thread','a','--thread','b')
        report = self.import_bundle()
        self.assertEqual(report['status_policy'],'preserve')
        with read_db(self.dest/t.STATE) as c:
            a = c.execute("SELECT * FROM threads WHERE id='a'").fetchone()
            b = c.execute("SELECT * FROM threads WHERE id='b'").fetchone()
            self.assertEqual(a['archived'],0)
            self.assertTrue(t.under(a['rollout_path'],self.dest/'sessions'))
            self.assertEqual(b['archived'],1)
            self.assertEqual(b['archived_at'],42)
            self.assertTrue(t.under(b['rollout_path'],self.dest/'archived_sessions'))
            self.assertEqual(json.loads(a['sandbox_policy']),{'type':'read-only'})

    def test_dry_run_creates_nothing(self):
        self.export('--thread','a')
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertFalse(self.dest.exists())

    def test_default_output_explains_selection_import_and_recovery(self):
        listed = call('codex-export.py','--codex-home',self.source,'--list-projects',json_output=False)
        self.assertEqual(listed.returncode,0,listed.stderr)
        self.assertIn('Alpha',listed.stdout)
        self.assertIn('native-alpha',listed.stdout)
        self.assertIn('archived and child/reviewer',listed.stdout)
        preview = call('codex-export.py','--codex-home',self.source,'--project','Alpha','--dry-run',json_output=False)
        self.assertEqual(preview.returncode,0,preview.stderr)
        self.assertIn('added history dependencies: 1',preview.stdout)
        self.assertFalse(self.bundle.exists())
        exported = call('codex-export.py','--codex-home',self.source,'--project','Alpha',
                        '--output',self.bundle,'--codex-stopped',json_output=False)
        self.assertEqual(exported.returncode,0,exported.stderr)
        self.assertIn('Exported: 2 thread(s)',exported.stdout)
        self.assertIn(str(self.bundle),exported.stdout)
        self.assertIn('private conversation history',exported.stdout)
        preview = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run',json_output=False)
        self.assertEqual(preview.returncode,0,preview.stderr)
        self.assertIn('no destination changes',preview.stdout)
        self.assertIn('Will import: 2',preview.stdout)
        self.assertFalse(self.dest.exists())
        imported = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--codex-stopped',json_output=False)
        self.assertEqual(imported.returncode,0,imported.stderr)
        self.assertIn('Import complete.',imported.stdout)
        self.assertIn('Backup and recovery receipt:',imported.stdout)
        skipped = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--skip-existing',json_output=False)
        self.assertEqual(skipped.returncode,0,skipped.stderr)
        self.assertIn('Nothing to import',skipped.stdout)

    def test_default_diagnosis_explains_blockers_and_missing_assets(self):
        initialize(self.dest)
        missing = self.source/'attachments'/'missing.png'
        with db(self.source/t.HISTORY) as c:
            c.execute("UPDATE thread_items SET item_json=? WHERE thread_id='a'",(json.dumps({'path':str(missing)}),))
        self.export('--thread','a')
        preview = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run',json_output=False)
        self.assertEqual(preview.returncode,0,preview.stderr)
        self.assertIn('Missing/empty referenced assets: 1',preview.stdout)
        self.assertIn(str(missing),preview.stdout)
        with db(self.dest/t.STATE) as c:
            c.execute('ALTER TABLE threads ADD COLUMN future INTEGER')
        diagnosed = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--diagnose',json_output=False)
        self.assertEqual(diagnosed.returncode,0,diagnosed.stderr)
        self.assertIn('INCOMPATIBLE',diagnosed.stdout)
        self.assertIn('tables:threads (changed)',diagnosed.stdout)
        self.assertIn('--diagnose --json',diagnosed.stdout)
        self.assertFalse((self.dest/'transfer-backups').exists())

    def test_no_arguments_show_getting_started_help_without_reading_storage(self):
        for script in ('codex-export.py','codex-import.py'):
            result = call(script,json_output=False)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('usage:',result.stdout)
            self.assertIn('--dry-run',result.stdout)
            self.assertIn('--json',result.stdout)

    def test_missing_stop_confirmation_rejected(self):
        self.export('--thread','a')
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest)
        self.assertNotEqual(r.returncode,0)
        self.assertFalse(self.dest.exists())

    def test_default_home_detects_desktop_and_cli_processes(self):
        ps = subprocess.CompletedProcess([], 0,
            f'100 {os.getuid()} /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n'
            f'101 {os.getuid()} /Applications/Codex.app/Contents/Resources/codex\n'
            f'102 {os.getuid()+1} /other/user/codex\n'
            '104 -2 /usr/libexec/dhcp6d\n'
            f'103 {os.getuid()} /Applications/ChatGPT.app/Helpers/browser_crashpad_handler\n', '')
        with patch.object(t, 'inspect_processes', return_value=ps):
            clients = t.running_clients(Path.home()/'.codex')
        self.assertEqual([x['pid'] for x in clients], [100,101])

    def test_custom_home_detects_any_open_database_holder(self):
        initialize(self.dest)
        ps = subprocess.CompletedProcess([], 0, f'200 {os.getuid()} /usr/bin/node\n', '')
        lsof = subprocess.CompletedProcess([], 0, f'p200\nf3\nn{self.dest/t.STATE}\n', '')
        with patch.object(t, 'inspect_processes', side_effect=[ps,lsof]), patch.object(t.shutil,'which',return_value='/usr/sbin/lsof'):
            clients = t.running_clients(self.dest)
        self.assertEqual([x['pid'] for x in clients], [200])
        self.assertEqual(clients[0]['process'], 'node')

    def test_other_home_clients_do_not_block_isolated_destination(self):
        initialize(self.dest)
        ps = subprocess.CompletedProcess([], 0, f'100 {os.getuid()} /Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n', '')
        lsof = subprocess.CompletedProcess([], 1, '', '')
        with patch.object(t, 'inspect_processes', side_effect=[ps,lsof]), patch.object(t.shutil,'which',return_value='/usr/sbin/lsof'):
            self.assertEqual(t.running_clients(self.dest), [])

    def test_process_inspection_failure_blocks_import(self):
        with patch.object(t.subprocess,'run',side_effect=PermissionError('process access denied')):
            with self.assertRaisesRegex(t.TransferError, 'process inspection failed'):
                t.ensure_codex_stopped(self.dest)
        initialize(self.dest)
        ps = subprocess.CompletedProcess([], 0, '', '')
        lsof = subprocess.CompletedProcess([], 1, '', 'permission denied')
        with patch.object(t, 'inspect_processes', side_effect=[ps,lsof]), patch.object(t.shutil,'which',return_value='/usr/sbin/lsof'):
            with self.assertRaisesRegex(t.TransferError, 'file-holder inspection'):
                t.ensure_codex_stopped(self.dest)

    def test_running_client_rejected_before_any_import_writes(self):
        self.export('--thread','a')
        argv=['codex-import.py',str(self.bundle),'--codex-home',str(self.dest),'--codex-stopped']
        with patch.object(sys,'argv',argv), patch.object(t,'running_clients',return_value=[{'pid':100,'process':'ChatGPT','reason':'client is running'}]):
            with self.assertRaisesRegex(t.TransferError, '--codex-stopped does not bypass'):
                t.import_main()
        self.assertFalse(self.dest.exists())

    def test_reopened_client_rejected_before_installation(self):
        initialize(self.dest)
        add_thread(self.dest,'existing',self.base/'other')
        self.export('--thread','a')
        before={name:t.hash_file(self.dest/name) for name in (t.STATE,t.HISTORY)}
        argv=['codex-import.py',str(self.bundle),'--codex-home',str(self.dest),'--codex-stopped']
        with patch.object(sys,'argv',argv), patch.object(t,'ensure_codex_stopped',side_effect=[None,t.TransferError('client reopened')]) as guard:
            with self.assertRaisesRegex(t.TransferError, 'client reopened'):
                t.import_main()
        self.assertEqual(guard.call_count,2)
        for name,h in before.items():
            self.assertEqual(t.hash_file(self.dest/name),h)
        self.assertEqual(list(self.dest.rglob('codex-transfer-*')),[])

    def test_read_only_modes_allowed_with_running_clients(self):
        initialize(self.dest)
        self.export('--thread','a')
        for mode in ('--dry-run','--diagnose'):
            argv=['codex-import.py',str(self.bundle),'--codex-home',str(self.dest),mode]
            with patch.object(sys,'argv',argv), patch.object(t,'ensure_codex_stopped',side_effect=AssertionError('must not check')) as guard, contextlib.redirect_stdout(io.StringIO()):
                t.import_main()
            guard.assert_not_called()

    def test_schema_mismatch_and_corruption_fail_before_changes(self):
        initialize(self.dest)
        self.export('--thread','a')
        with db(self.dest/t.STATE) as c:
            c.execute('ALTER TABLE threads ADD COLUMN future INTEGER')
        before = t.hash_file(self.dest/t.STATE)
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--codex-stopped')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('schema/migrations differ',r.stderr)
        self.assertEqual(t.hash_file(self.dest/t.STATE),before)
        with (self.bundle/'global-state.json').open('a') as f:
            f.write('corruption')
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run')
        self.assertIn('checksum/size mismatch',r.stderr)

    def test_sql_quoting_and_formatting_are_compatible(self):
        initialize(self.dest)
        self.export('--thread','a')
        with db(self.dest/t.STATE) as c:
            c.execute('PRAGMA writable_schema=ON')
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name='threads'").fetchone()[0]
            sql = sql.replace('CREATE TABLE threads', 'CREATE  TABLE "threads"')+';'
            c.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name='threads'",(sql,))
            c.execute('PRAGMA writable_schema=OFF')
        report = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--diagnose')
        self.assertEqual(report.returncode,0,report.stderr)
        details=json.loads(report.stdout)['databases'][t.STATE]
        self.assertTrue(details['compatible'])
        self.assertFalse(details['raw_signature_matches'])
        self.import_bundle()

    def test_nonunique_index_difference_is_compatible(self):
        initialize(self.dest)
        self.export('--thread','a')
        with db(self.dest/t.STATE) as c:
            c.execute('CREATE INDEX extra_performance_index ON threads(title)')
        report = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--diagnose')
        self.assertEqual(report.returncode,0,report.stderr)
        details=json.loads(report.stdout)['databases'][t.STATE]
        self.assertTrue(details['compatible'])
        self.assertTrue(details['nonblocking_index_differences'])
        self.import_bundle()

    def test_unique_index_difference_is_rejected(self):
        initialize(self.dest)
        self.export('--thread','a')
        with db(self.dest/t.STATE) as c:
            c.execute('CREATE UNIQUE INDEX changed_constraint ON threads(title)')
        before=t.hash_file(self.dest/t.STATE)
        r=call('codex-import.py',self.bundle,'--codex-home',self.dest,'--codex-stopped')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('indexes:changed_constraint',r.stderr)
        self.assertEqual(t.hash_file(self.dest/t.STATE),before)

    def test_destination_only_sequence_is_compatible_and_preserved(self):
        initialize(self.dest)
        add_thread(self.dest,'existing',self.base/'elsewhere')
        add_obsolete_sequence(self.dest/t.STATE)
        with read_db(self.dest/t.STATE) as c:
            before_sequence = [tuple(r) for r in c.execute('SELECT * FROM sqlite_sequence')]
            before_thread = dict(c.execute("SELECT * FROM threads WHERE id='existing'").fetchone())
        self.export('--thread','a')
        before_hash = t.hash_file(self.dest/t.STATE)
        report = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--diagnose')
        self.assertEqual(report.returncode,0,report.stderr)
        details = json.loads(report.stdout)['databases'][t.STATE]
        self.assertTrue(details['compatible'])
        self.assertFalse(details['raw_signature_matches'])
        self.assertEqual(details['blocking_differences'],[])
        self.assertEqual(details['ignored_bookkeeping_differences'],
                         [{'kind':'tables','name':'sqlite_sequence','difference':'destination_only'}])
        self.assertEqual(t.hash_file(self.dest/t.STATE),before_hash)
        self.import_bundle()
        with read_db(self.dest/t.STATE) as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM sqlite_sequence')],before_sequence)
            self.assertEqual(dict(c.execute("SELECT * FROM threads WHERE id='existing'").fetchone()),before_thread)
            self.assertIsNotNone(c.execute("SELECT id FROM threads WHERE id='a'").fetchone())

    def test_source_sequence_export_filters_stale_counters(self):
        add_obsolete_sequence(self.source/t.STATE)
        before = t.hash_file(self.source/t.STATE)
        self.export('--thread','a')
        self.assertEqual(t.hash_file(self.source/t.STATE),before)
        with read_db(self.bundle/t.STATE) as c:
            if 'sqlite_sequence' in t.tables(c):
                self.assertEqual(c.execute('SELECT count(*) FROM sqlite_sequence').fetchone()[0],0)
        initialize(self.dest)
        self.import_bundle()

    def test_unknown_application_table_still_blocks_import(self):
        initialize(self.dest)
        self.export('--thread','a')
        with db(self.dest/t.STATE) as c:
            c.execute('CREATE TABLE unexpected_application_data (value TEXT)')
        before = t.hash_file(self.dest/t.STATE)
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('tables:unexpected_application_data(destination_only)',r.stderr)
        self.assertEqual(t.hash_file(self.dest/t.STATE),before)

    def test_migration_difference_report_and_no_destination_writes(self):
        initialize(self.dest)
        self.export('--thread','a')
        with db(self.dest/t.STATE) as c:
            c.execute("INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES(59,'target-only-migration',1,?,0)",(b'other',))
        before=t.hash_file(self.dest/t.STATE)
        r=call('codex-import.py',self.bundle,'--codex-home',self.dest,'--diagnose')
        self.assertEqual(r.returncode,0,r.stderr)
        details=json.loads(r.stdout)['databases'][t.STATE]
        self.assertFalse(details['compatible'])
        self.assertIn({'kind':'migrations','name':'59','difference':'destination_only'},details['blocking_differences'])
        self.assertEqual(t.hash_file(self.dest/t.STATE),before)
        self.assertFalse((self.dest/'transfer-backups').exists())

    def test_migration_checksum_difference_is_rejected(self):
        initialize(self.dest)
        self.export('--thread','a')
        with db(self.dest/t.STATE) as c:
            c.execute('UPDATE _sqlx_migrations SET checksum=? WHERE version=1',(b'other',))
        r=call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('migrations:1(changed)',r.stderr)

    def test_missing_manifest_has_specific_error(self):
        self.bundle.mkdir()
        r=call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('manifest.json missing',r.stderr)

    def test_rollback_after_database_replace_failure(self):
        initialize(self.dest)
        add_thread(self.dest,'existing',self.base/'elsewhere')
        self.export('--thread','a')
        with read_db(self.dest/t.STATE) as c:
            before = [dict(x) for x in c.execute('SELECT * FROM threads')]
        replace = t.os.replace
        def broken_replace(src, dst):
            if Path(dst) == self.dest/t.HISTORY:
                raise OSError('simulated disk error')
            return replace(src,dst)
        argv = ['codex-import.py',str(self.bundle),'--codex-home',str(self.dest),'--codex-stopped']
        with patch.object(sys,'argv',argv),patch.object(t.os,'replace',broken_replace),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                t.import_main()
        with read_db(self.dest/t.STATE) as c:
            self.assertEqual([dict(x) for x in c.execute('SELECT * FROM threads')],before)
        self.assertEqual(list((self.dest/'archived_sessions').rglob('*.jsonl')),[])

    def test_bundle_path_traversal_rejected(self):
        self.export('--thread','a')
        m = t.read_json(self.bundle/'manifest.json')
        m['files']['../escape'] = {'bytes':0,'sha256':'x'}
        t.dump(self.bundle/'manifest.json',m)
        r = call('codex-import.py',self.bundle,'--codex-home',self.dest,'--dry-run')
        self.assertIn('Unsafe bundle path',r.stderr)
        self.assertFalse(self.dest.exists())


if __name__ == '__main__':
    unittest.main()
