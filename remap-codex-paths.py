#!/usr/bin/env python3
"""Repair local Codex project/thread paths offline, preserving session bytes."""
import argparse
import contextlib
import copy
import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

import transfer as t


def records(c):
    return {name: [tuple(r) for r in c.execute('SELECT * FROM '+t.qi(name)+' ORDER BY rowid')]
            for name in sorted(t.tables(c))}


def rewrite_paths(value, maps):
    if isinstance(value, str):
        return t.mapped(value, maps)
    if isinstance(value, list):
        return [rewrite_paths(v, maps) for v in value]
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            new_key = t.mapped(k, maps)
            if new_key in result:
                t.fail('Path-key collision in app metadata')
            result[new_key] = rewrite_paths(v, maps)
        return result
    return value


def plan(home, maps):
    t.source_paths(home)
    for name in (t.STATE, t.HISTORY, t.GLOBAL):
        if (home/name).is_symlink():
            t.fail('Managed files must not be symlinks')
    original = (home/t.GLOBAL).read_bytes()
    before_global = json.loads(original)
    after_global = copy.deepcopy(before_global)
    with contextlib.closing(t.ro(home/t.STATE)) as c:
        t.validate_schema(c, t.STATE_TABLES, t.STATE)
        t.check_db(c)
        before = records(c)
        signature = t.schema_signature(c)
        columns = {table:[r[1] for r in c.execute('PRAGMA table_info('+t.qi(table)+')')]
                   for table in before}
        projects = {r['id']:r['name'] for r in c.execute('SELECT id,name FROM projects')}
        roots = [dict(r) for r in c.execute('SELECT * FROM project_roots ORDER BY project_id,position')]
        threads = [dict(r) for r in c.execute('SELECT * FROM threads')]
    root_changes = []
    thread_changes = []
    for r in roots:
        new = t.mapped(r['path'], maps)
        if new != r['path']:
            if not Path(new).is_dir():
                t.fail('Mapped project folder does not exist: '+new)
            root_changes.append(dict(project_id=r['project_id'], name=projects[r['project_id']],
                                     position=r['position'], old=r['path'], new=new))
    final_roots = {}
    for r in roots:
        final_roots.setdefault(r['project_id'], []).append(t.mapped(r['path'],maps))
    root_sets = {}
    for pid, values in final_roots.items():
        key = tuple(sorted(values))
        if len(set(values)) != len(values) or key in root_sets:
            t.fail('Remapping would leave duplicate project roots')
        root_sets[key] = pid
    for r in threads:
        new = t.mapped(r['cwd'], maps)
        if new != r['cwd']:
            if not Path(new).is_dir():
                t.fail('Mapped thread working directory does not exist: '+new)
            thread_changes.append(dict(id=r['id'], old=r['cwd'], new=new, rollout_path=r['rollout_path']))
    for p in after_global.get('local-projects',{}).values():
        if 'rootPaths' in p:
            p['rootPaths'] = rewrite_paths(p['rootPaths'], maps)
    for key in ('thread-workspace-root-hints','thread-writable-roots','thread-projectless-output-directories'):
        if key in after_global:
            after_global[key] = rewrite_paths(after_global[key], maps)
    # Cached applied/pending workspace state is live path metadata, not history.
    atom = after_global.get('electron-persisted-atom-state',{})
    for key in atom:
        if key.startswith('thread-workspace-state-v1:'):
            atom[key] = rewrite_paths(atom[key], maps)
    expected = copy.deepcopy(before)
    for table, column in (('threads','cwd'),('project_roots','path')):
        index = columns[table].index(column)
        updated = []
        for row in expected[table]:
            values = list(row)
            values[index] = t.mapped(values[index],maps)
            updated.append(tuple(values))
        expected[table] = updated
    return {'original_global':original,'after_global':after_global,'before':before,'expected':expected,
            'signature':signature,'report':{'project_roots':root_changes,
            'updated_threads':len(thread_changes),
            'app_metadata_changed':after_global != before_global,
            'path_maps':[{'old':old,'new':new} for old,new in maps]},'threads':thread_changes}


def history_hashes(home, thread_changes):
    paths = {Path(r['rollout_path']) if Path(r['rollout_path']).is_absolute() else home/r['rollout_path']
             for r in thread_changes}
    paths.update(home/name for name in (t.HISTORY, t.HISTORY+'-wal') if (home/name).exists())
    return {str(path):t.hash_file(path) for path in paths}


def verify_history(hashes):
    if any(t.hash_file(Path(path)) != value for path,value in hashes.items()):
        t.fail('History file changed during path repair')


def repair(home, maps, apply=False):
    home = home.expanduser().resolve()
    if apply:
        t.ensure_codex_stopped(home)
    p = plan(home,maps)
    result = dict(p['report'],destination=str(home),dry_run=not apply)
    if not apply or (not result['project_roots'] and not result['updated_threads'] and not result['app_metadata_changed']):
        return result
    hashes = history_hashes(home,p['threads'])
    previous_umask = os.umask(0o077)
    backup = home/'transfer-backups'/('path-repair-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+str(uuid.uuid4()))
    installing = False
    try:
        backup.mkdir(parents=True)
        t.backup(home/t.STATE,backup/t.STATE)
        (backup/t.GLOBAL).write_bytes(p['original_global'])
        staged = backup/'remapped-state.sqlite'
        shutil.copyfile(backup/t.STATE,staged)
        with contextlib.closing(sqlite3.connect(staged)) as c:
            with c:
                for r in p['report']['project_roots']:
                    c.execute('UPDATE project_roots SET path=? WHERE project_id=? AND position=? AND path=?',
                              (r['new'],r['project_id'],r['position'],r['old']))
                for r in p['threads']:
                    c.execute('UPDATE threads SET cwd=? WHERE id=? AND cwd=?',(r['new'],r['id'],r['old']))
            c.execute('PRAGMA journal_mode=DELETE')
            t.check_db(c)
            if records(c) != p['expected'] or t.schema_signature(c) != p['signature']:
                t.fail('Unexpected changes in staged metadata database')
        staged_global = backup/'remapped-global.json'
        t.dump(staged_global,p['after_global'])
        for source,target in ((home/t.STATE,staged),(home/t.GLOBAL,staged_global)):
            shutil.copymode(source,target)
        t.ensure_codex_stopped(home)
        if (home/t.GLOBAL).read_bytes() != p['original_global']:
            t.fail('App metadata changed during preparation')
        with contextlib.closing(t.ro(home/t.STATE)) as c:
            if records(c) != p['before'] or t.schema_signature(c) != p['signature']:
                t.fail('Database changed during preparation')
        verify_history(hashes)
        receipt = dict(result,backup=str(backup),history_hashes=hashes,status='prepared')
        t.dump(backup/'receipt.json',receipt)
        installing = True
        for suffix in ('-wal','-shm'):
            side = home/(t.STATE+suffix)
            if side.exists():
                os.replace(side,backup/(t.STATE+suffix+'.original-sidecar'))
        os.replace(staged,home/t.STATE)
        os.replace(staged_global,home/t.GLOBAL)
        with contextlib.closing(t.ro(home/t.STATE)) as c:
            t.check_db(c)
            if records(c) != p['expected']:
                t.fail('Installed path metadata differs from staged plan')
        if t.read_json(home/t.GLOBAL) != p['after_global']:
            t.fail('Installed app metadata differs from staged plan')
        verify_history(hashes)
        t.dump(backup/'receipt.json',dict(receipt,status='complete'))
        return dict(result,backup=str(backup),status='complete',verified_history_files=len(hashes))
    except BaseException:
        if installing:
            for suffix in ('-wal','-shm'):
                (home/(t.STATE+suffix)).unlink(missing_ok=True)
            shutil.copyfile(backup/t.STATE,home/t.STATE)
            shutil.copyfile(backup/t.GLOBAL,home/t.GLOBAL)
            t.dump(backup/'receipt.json',dict(result,status='rolled-back'))
        raise
    finally:
        os.umask(previous_umask)


def main():
    p = argparse.ArgumentParser(description=__doc__,epilog='Default: read-only preview. --apply requires stopped clients. Historical session text is never rewritten.')
    p.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))),
                   help='Codex storage directory (default: CODEX_HOME, or ~/.codex)')
    p.add_argument('--path-map',action='append',required=True,help='OLD=NEW absolute folder paths; repeatable')
    p.add_argument('--apply',action='store_true',help='Back up and install the verified path changes')
    p.add_argument('--codex-stopped',action='store_true',help='Acknowledge stopped clients; automatic checks still run before applying')
    a = p.parse_args()
    if a.apply and not a.codex_stopped:
        p.error('--apply requires --codex-stopped')
    print(json.dumps(repair(a.codex_home,t.path_maps(a.path_map),a.apply),indent=2))


if __name__ == '__main__':
    t.run(main)
