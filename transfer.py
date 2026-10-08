"""Local, standard-library-only Codex history transfer. No Codex/Git subprocesses."""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

FORMAT = "codex-history-bundle-v1"
VERSION = "1.4.0"
STATE = "state_5.sqlite"
HISTORY = "thread_history_1.sqlite"
GLOBAL = ".codex-global-state.json"
STATE_TABLES = {
    "_sqlx_migrations", "threads", "thread_dynamic_tools", "backfill_state",
    "thread_spawn_edges", "remote_control_enrollments", "external_agent_config_imports",
    "thread_sections", "rollout_migration_state", "rollout_migration_skipped_rollouts",
    "projects", "project_roots", "project_idempotency_keys", "thread_attachments",
}
HISTORY_TABLES = {
    "_sqlx_migrations", "thread_turns", "thread_items", "thread_realtime_items",
    "thread_history_projection_state",
}
# SQLite keeps this AUTOINCREMENT bookkeeping table after its last user table
# is dropped. Its presence is not part of Codex's application schema.
SQLITE_BOOKKEEPING_TABLES = {"sqlite_sequence"}
STATE_THREAD_TABLES = ("thread_dynamic_tools", "thread_attachments")
ASSET_DIRS = {"attachments", "generated_images", "visualizations"}
PATH_KEYS = {"path", "file_path", "image_path", "imagePath", "filePath", "url", "image_url"}


class TransferError(Exception):
    pass


def fail(message):
    raise TransferError(message)


def inspect_processes(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        fail(f"Cannot verify that Codex is stopped: {Path(command[0]).name} process inspection failed ({type(error).__name__}). Nothing will be imported.")


def running_clients(home):
    """Check the default-home app/CLI processes plus open destination managed files."""
    if sys.platform not in ("darwin", "linux"):
        fail("Automatic stopped-client checks currently support macOS and Linux only; import is blocked on this platform.")
    result = inspect_processes(["ps", "-ax", "-o", "pid=,uid=,comm="])
    if result.returncode != 0 or result.stderr.strip():
        fail("Cannot verify that Codex is stopped: ps process inspection was denied or failed. Nothing will be imported.")
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit() or re.fullmatch(r"-?\d+", fields[1]) is None:
            fail("Cannot verify that Codex is stopped: unrecognized ps output. Nothing will be imported.")
        processes[int(fields[0])] = (int(fields[1]), Path(fields[2]).name)
    blockers = {}
    if home.resolve() == (Path.home()/".codex").resolve():
        for pid, (uid, name) in processes.items():
            if uid == os.getuid() and name in ("ChatGPT", "Codex", "codex"):
                blockers[pid] = {"pid": pid, "process": name, "reason": "Codex/ChatGPT client is running"}
    if blockers:
        return [blockers[pid] for pid in sorted(blockers)]
    managed = [home/name for name in (STATE, HISTORY, GLOBAL,
               STATE+"-wal", STATE+"-shm", HISTORY+"-wal", HISTORY+"-shm") if (home/name).exists()]
    if managed:
        lsof = shutil.which("lsof")
        if lsof is None:
            fail("Cannot verify that Codex is stopped: lsof is unavailable. Install lsof before importing. Nothing will be imported.")
        result = inspect_processes([lsof, "-nP", "-Fpn", "--", *map(str, managed)])
        # lsof returns 1 with empty output when no process has these files open.
        if result.returncode not in (0, 1) or result.stderr.strip() or (result.returncode == 0 and not result.stdout.strip()):
            fail("Cannot verify that Codex is stopped: lsof file-holder inspection was denied or failed. Nothing will be imported.")
        pid = None
        for line in result.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = int(line[1:])
            elif line.startswith("f") and len(line) > 1 and pid is not None:
                # lsof includes the file-descriptor marker even with -Fpn.
                continue
            elif line.startswith("n") and pid is not None:
                blockers[pid] = {"pid": pid, "process": processes.get(pid, (None, "unknown"))[1],
                                 "reason": "destination Codex database or metadata file is open"}
            elif line and not line.startswith("n"):
                fail("Cannot verify that Codex is stopped: unrecognized lsof output. Nothing will be imported.")
        if result.stdout.strip() and not any(line.startswith("n") for line in result.stdout.splitlines()):
            fail("Cannot verify that Codex is stopped: lsof did not identify open files. Nothing will be imported.")
    return [blockers[pid] for pid in sorted(blockers)]


def ensure_codex_stopped(home):
    clients = running_clients(home)
    if clients:
        descriptions = "; ".join(f"{x['process']} (PID {x['pid']}): {x['reason']}" for x in clients)
        fail("Destination Codex clients are still running: " + descriptions +
             ". Quit them and retry. --codex-stopped does not bypass this automatic check.")


def dump(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding="utf-8"))


def ro(path):
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def validate_schema(c, expected, label):
    actual = tables(c) - SQLITE_BOOKKEEPING_TABLES
    if actual != expected:
        fail(f"Unsupported {label} tables: extra={sorted(actual-expected)}, missing={sorted(expected-actual)}")


def schema_signature(c):
    schema = [list(r) for r in c.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
    migrations = [list(r[:3]) for r in c.execute(
        "SELECT version,description,hex(checksum) FROM _sqlx_migrations ORDER BY version")]
    return hashlib.sha256(json.dumps([schema, migrations], sort_keys=True).encode()).hexdigest()


def sql_tokens(sql):
    """Normalize SQL spelling only; preserve quoted string values and expressions."""
    pattern = r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?|[^\s]"
    result = []
    for token in re.findall(pattern, sql or ""):
        if token.startswith(("--", "/*")):
            continue
        if token.startswith("'"):
            result.append(token)
        elif token[:1] in ('"', '`', '[') and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", token[1:-1]):
            result.append(token[1:-1].lower())
        else:
            result.append(token.lower())
    if result and result[-1] == ";":
        result.pop()
    return result


def structural_schema(c):
    """Inspect layout AND constraints/trigger SQL; don't infer compatibility from columns alone."""
    result = {"tables": {}, "triggers": {}, "indexes": {}, "migrations": {}}
    application_tables = tables(c) - SQLITE_BOOKKEEPING_TABLES
    for table in sorted(application_tables):
        ddl = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        cols = []
        for row in c.execute(f"PRAGMA table_xinfo({qi(table)})"):
            values = list(row)
            values[2] = values[2].upper()
            values[4] = sql_tokens(values[4]) if values[4] is not None else None
            cols.append(values)
        fks = sorted([list(row) for row in c.execute(f"PRAGMA foreign_key_list({qi(table)})")])
        result["tables"][table] = {"columns": cols, "foreign_keys": fks, "ddl": sql_tokens(ddl)}
    for name, table, sql in c.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"):
        result["triggers"][name] = {"table": table, "sql": sql_tokens(sql)}
    for table in sorted(application_tables):
        for index in c.execute(f"PRAGMA index_list({qi(table)})"):
            name = index[1]
            sql = c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()[0]
            result["indexes"][name] = {"table": table, "unique": bool(index[2]), "origin": index[3],
                "partial": bool(index[4]), "columns": [list(r) for r in c.execute(f"PRAGMA index_xinfo({qi(name)})")],
                "sql": sql_tokens(sql)}
    for row in c.execute("SELECT version,description,success,hex(checksum) FROM _sqlx_migrations ORDER BY version"):
        result["migrations"][str(row[0])] = {"description": row[1], "success": row[2], "checksum": row[3]}
    return result


def schema_comparison(source, destination):
    a, b = structural_schema(source), structural_schema(destination)
    blocking = []
    notes = []
    for kind in ("tables", "triggers", "migrations"):
        for key in sorted(set(a[kind]) | set(b[kind])):
            if a[kind].get(key) == b[kind].get(key):
                continue
            detail = {"kind": kind, "name": key}
            if key not in a[kind]:
                detail["difference"] = "destination_only"
            elif key not in b[kind]:
                detail["difference"] = "bundle_only"
            else:
                detail["difference"] = "changed"
                detail["bundle"] = a[kind][key]
                detail["destination"] = b[kind][key]
            blocking.append(detail)
    # Nonunique index variations affect query performance, not inserted data.
    # Unique/constraint index differences always block the merge.
    for name in sorted(set(a["indexes"]) | set(b["indexes"])):
        old, new = a["indexes"].get(name), b["indexes"].get(name)
        if old != new:
            detail = {"kind": "indexes", "name": name, "bundle": old, "destination": new}
            (blocking if any(x and x["unique"] for x in (old, new)) else notes).append(detail)
    bookkeeping = [{"kind": "tables", "name": name,
                    "difference": "bundle_only" if name in tables(source) else "destination_only"}
                   for name in sorted((tables(source) ^ tables(destination)) & SQLITE_BOOKKEEPING_TABLES)]
    return {"compatible": not blocking, "raw_signature_matches": schema_signature(source) == schema_signature(destination),
            "blocking_differences": blocking, "nonblocking_index_differences": notes,
            "ignored_bookkeeping_differences": bookkeeping}


def compatibility_report(home, bundle):
    report = {"destination": str(home), "bundle": str(bundle), "databases": {}}
    for name in (STATE, HISTORY):
        if not (home/name).exists():
            report["databases"][name] = {"destination_database_missing": True}
            continue
        with contextlib.closing(ro(bundle/name)) as src, contextlib.closing(ro(home/name)) as dst:
            comparison = schema_comparison(src, dst)
            comparison["bundle_migration_versions"] = [r[0] for r in src.execute("SELECT version FROM _sqlx_migrations ORDER BY version")]
            comparison["destination_migration_versions"] = [r[0] for r in dst.execute("SELECT version FROM _sqlx_migrations ORDER BY version")]
            report["databases"][name] = comparison
    return report


def check_db(c):
    result = [r[0] for r in c.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        fail("SQLite integrity check failed: " + str(result[:5]))
    errors = [list(r) for r in c.execute("PRAGMA foreign_key_check")]
    if errors:
        fail("Foreign-key check failed: " + str(errors[:5]))


def backup(source, dest):
    with contextlib.closing(ro(source)) as src, contextlib.closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")


def under(path, root):
    # Lexical comparison: never resolves or accesses project directories.
    p, r = os.path.normpath(str(path)), os.path.normpath(str(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def home_arg(parser):
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))),
                        help="Codex storage directory (default: CODEX_HOME, or ~/.codex)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of explanatory output")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)


def human_bytes(size):
    return f"{size:,} bytes ({size / (1024 * 1024):.1f} MiB)"


def report(kind, value, args, home):
    """Render CLI results; --json keeps the original payloads for scripts."""
    if args.json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return
    print(f"Codex home: {home}")
    if kind == "projects":
        projects = value["projects"]
        print(f"\nFound {len(projects)} local project(s). Thread counts include archived and child/reviewer threads.")
        for project in projects:
            print(f"\n{project['name']} — {project['thread_count']} thread(s)")
            print(f"  ID: {project['id']}")
            for root in project["roots"]:
                print(f"  Root: {root}")
            if len(project["aliases"]) > 1:
                print("  Aliases: " + ", ".join(project["aliases"]))
        print("\nNext: preview a selection with --project NAME_OR_ID --dry-run.")
    elif kind == "threads":
        print(f"\nFound {len(value['threads'])} stored thread(s), including archived and child/reviewer threads.")
        for thread in value["threads"]:
            status = "archived" if thread["archived"] else "active"
            print(f"\n{thread['title']} [{status}]")
            print(f"  ID: {thread['id']}")
            print(f"  Project: {thread['project'] or '(unassigned)'}")
            print(f"  Working directory: {thread['cwd']}")
        print("\nNext: preview a selection with --thread THREAD_ID --dry-run.")
    elif kind == "export-preview":
        print("\nExport preview — no bundle created.")
        print(f"Selected: {value['primary_threads']} thread(s); added history dependencies: {value['dependency_threads']}.")
        print("Session data: " + human_bytes(value["session_bytes"]) + "; databases and assets are additional.")
        for project in value["projects"]:
            print(f"  Project: {project['name']} ({project['id']})")
        print("Thread IDs: " + ", ".join(value["thread_ids"]))
        print("Dependencies can include child/reviewer threads or parent/fork history from another project.")
        print("Next: quit all source Codex clients, then repeat the selection with --output NEW_BUNDLE_DIR --codex-stopped.")
    elif kind == "export":
        print("\nExport complete.")
        print(f"Bundle: {value['bundle']}")
        print(f"Exported: {value['thread_count']} thread(s), including {value['dependency_count']} added dependency thread(s).")
        print("Payload size: " + human_bytes(value["bytes"]))
        print(f"Snapshot: {value['snapshot']}")
        if value["snapshot"] != "offline":
            print("This live snapshot is not atomic across databases and session files; re-export offline for migration.")
        report_missing_assets(value["missing_assets"])
        print("Next: copy this entire bundle directory to the destination, then run codex-import.py BUNDLE_DIR --dry-run.")
        print("The bundle contains private conversation history. Source repositories were not copied.")
    elif kind == "diagnose":
        print(f"\nSchema comparison for bundle: {value['bundle']}")
        for name, comparison in value["databases"].items():
            if comparison.get("destination_database_missing"):
                print(f"  {name}: absent; an import into an empty destination can initialize its schema.")
                continue
            status = "compatible" if comparison["compatible"] else "INCOMPATIBLE"
            print(f"  {name}: {status}")
            for difference in comparison["blocking_differences"]:
                print(f"    Blocking: {difference['kind']}:{difference['name']} ({difference.get('difference', 'changed')})")
            for difference in comparison["nonblocking_index_differences"]:
                print(f"    Allowed performance-index difference: {difference['name']}")
            for difference in comparison["ignored_bookkeeping_differences"]:
                print(f"    Allowed SQLite bookkeeping difference: {difference['name']}")
        print("No history imported. Schema compatibility does not check for duplicate thread IDs or path conflicts.")
        print("Next: use --dry-run to preview the import, or --diagnose --json for full migration/constraint details.")
    elif kind in ("import-preview", "import"):
        if kind == "import-preview":
            print("\nImport preview — no destination changes.")
        else:
            print("\nImport complete.")
        verb = "Will import" if kind == "import-preview" else "Imported"
        print(f"{verb}: {value['import_threads']} thread(s); skipped existing IDs: {value['skip_threads']}.")
        print("Counts include stored child/reviewer threads; the app may show fewer chats.")
        print("Chat status: " + ("preserve source active/archived status" if value["status_policy"] == "preserve" else "archive all imported chats"))
        print("Imported thread defaults: read-only sandbox, unpinned, memories and Daybreak disabled.")
        for pid, roots in value["project_roots"].items():
            print(f"  Project {pid}: " + (", ".join(roots) or "(no registered roots)"))
        print("Project registrations are created or reused. Source folders are not copied or modified.")
        print(f"Source snapshot: {value['source_snapshot']}")
        if value["source_snapshot"] != "offline":
            print("This bundle came from a non-atomic live snapshot; prefer an offline export for migration.")
        report_missing_assets(value["missing_assets"])
        if value["import_threads"] == 0:
            print("Nothing to import; the destination was left unchanged.")
        elif kind == "import-preview":
            print("Next: confirm the mapped paths, quit destination Codex clients, and replace --dry-run with --codex-stopped.")
            print("A modifying import checks for running clients twice; this read-only preview does not.")
        else:
            print(f"Backup and recovery receipt: {value['backup']}")
            print("Next: reopen Codex and inspect the imported chats. Archived chats remain in Archived; child/reviewer threads may be hidden.")


def report_missing_assets(paths):
    if not paths:
        return
    print(f"Missing/empty referenced assets: {len(paths)}. These optional assets were absent from the export; history may refer to unavailable files.")
    for path in paths:
        print("  " + path)


def source_paths(home):
    if not (home/STATE).is_file() or not (home/HISTORY).is_file():
        fail(f"This tool supports {STATE} and {HISTORY}; initialize a compatible Codex installation first.")
    for stem, name in [("state_", STATE), ("thread_history_", HISTORY)]:
        extras = [p.name for p in home.glob(stem + "*.sqlite") if p.name != name]
        if extras:
            fail(f"Different database versions detected: {extras}. Update this tool for that schema.")


def catalog(home, c, global_state):
    projects = {}
    aliases = {}
    for row in c.execute("SELECT * FROM projects"):
        pid = row["id"]
        roots = [r[0] for r in c.execute("SELECT path FROM project_roots WHERE project_id=? ORDER BY position", (pid,))]
        projects[pid] = {"id": pid, "name": row["name"], "roots": roots, "aliases": [pid]}
        aliases[pid] = pid
    host_maps = global_state.get("app-server-project-id-by-legacy-project-id-by-host", {})
    host_map = {}
    for host_key, entries in host_maps.items():
        if host_key.startswith("local:") and Path(host_key[6:]).expanduser().resolve() == home.resolve():
            host_map.update(entries)
    for old, target in host_map.items():
        if target in projects:
            aliases[old] = target
            projects[target]["aliases"].append(old)
    for old, value in global_state.get("local-projects", {}).items():
        target = aliases.get(old, old)
        if target not in projects:
            projects[target] = {"id": target, "name": value.get("name", old),
                                "roots": value.get("rootPaths", []), "aliases": [old]}
        aliases[old] = target
    return projects, aliases


def owner(row, global_state, projects, aliases):
    explicit = row.get("project_id")
    assignment = global_state.get("thread-project-assignments", {}).get(row["id"], {})
    if assignment.get("projectKind") == "local":
        explicit = explicit or assignment.get("projectId")
    if explicit:
        return aliases.get(explicit, explicit)
    candidates = [(len(root), pid) for pid, p in projects.items() for root in p["roots"] if under(row["cwd"], root)]
    return max(candidates)[1] if candidates else None


def first_metadata(path):
    if not path.is_file():
        fail("Session file missing: " + str(path))
    with path.open("rb") as f:
        line = f.readline()
    try:
        item = json.loads(line)
    except (ValueError, UnicodeError):
        fail("Invalid session header: " + str(path))
    return item.get("payload", {}) if item.get("type") == "session_meta" else {}


def selection(home, c, g, args):
    projects, aliases = catalog(home, c, g)
    threads = {r["id"]: dict(r) for r in c.execute("SELECT * FROM threads")}
    owners = {tid: owner(row, g, projects, aliases) for tid, row in threads.items()}
    if args.list_projects:
        return {"projects": [dict(p, thread_count=sum(x == pid for x in owners.values()))
                             for pid, p in sorted(projects.items(), key=lambda x: x[1]["name"].lower())]}
    if args.list_threads:
        return {"threads": [{"id": tid, "title": row.get("name") or row["title"], "cwd": row["cwd"],
                             "project": projects.get(owners[tid], {}).get("name"), "archived": bool(row["archived"])}
                            for tid, row in threads.items()]}
    primary = set(args.thread or [])
    missing = primary - threads.keys()
    if missing:
        fail("Unknown thread IDs: " + ", ".join(sorted(missing)))
    selected_projects = set()
    for token in args.project or []:
        matches = {pid for pid, p in projects.items() if token in p["aliases"] or token == p["name"] or token in p["roots"]}
        if len(matches) != 1:
            fail(f"Project {token!r} matches {len(matches)} projects; use an ID from --list-projects.")
        selected_projects.update(matches)
    primary.update(tid for tid, pid in owners.items() if pid in selected_projects)
    for root in args.cwd or []:
        primary.update(tid for tid, row in threads.items() if under(row["cwd"], root))
    if not primary:
        fail("No threads selected. Supply --project, --thread, or --cwd; use --list-projects/--list-threads first.")
    edges = [dict(r) for r in c.execute("SELECT * FROM thread_spawn_edges")]
    children = {}
    parents = {}
    for e in edges:
        children.setdefault(e["parent_thread_id"], set()).add(e["child_thread_id"])
        parents.setdefault(e["child_thread_id"], set()).add(e["parent_thread_id"])
    # Include descendants of the originally selected threads, not siblings of dependencies.
    included = set(primary)
    pending = list(primary)
    while pending:
        for tid in children.get(pending.pop(), set()):
            if tid not in threads:
                fail("Spawn dependency missing from state database: " + tid)
            if tid not in included:
                included.add(tid)
                pending.append(tid)
    pending = list(included)
    metadata = {}
    while pending:
        tid = pending.pop()
        safe_thread_id(tid)
        row = threads[tid]
        path = Path(row["rollout_path"])
        if not path.is_absolute():
            path = home/path
        # Avoid ever reading code or a substituted arbitrary file as a rollout.
        if not any(under(path.resolve(), (home/d).resolve()) for d in ("sessions", "archived_sessions")):
            fail("Session path outside Codex session storage: " + str(path))
        meta = first_metadata(path)
        if meta.get("id") != tid:
            fail("Session header ID does not match state database: " + tid)
        metadata[tid] = meta
        deps = set(parents.get(tid, set()))
        for key in ("forked_from_id", "session_id"):
            if meta.get(key) and meta[key] != tid:
                deps.add(meta[key])
        for dep in deps:
            if dep not in threads:
                fail(f"Required parent/root thread {dep} for {tid} is absent; cannot create a self-contained bundle.")
            if dep not in included:
                included.add(dep)
                pending.append(dep)
    selected_projects.update(owners[tid] for tid in included if owners[tid] in projects)
    return {"primary_ids": sorted(primary), "thread_ids": sorted(included),
            "dependency_ids": sorted(included-primary),
            "projects": [projects[pid] for pid in sorted(selected_projects)],
            "owners": {tid: owners[tid] for tid in included},
            "threads": {tid: threads[tid] for tid in included}, "metadata": metadata}


def prune_database(path, keep_ids, project_ids, is_state):
    expected = STATE_TABLES if is_state else HISTORY_TABLES
    with contextlib.closing(sqlite3.connect(path)) as c:
        validate_schema(c, expected, path.name)
        c.execute("PRAGMA secure_delete=ON")
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("CREATE TEMP TABLE selected_threads(id TEXT PRIMARY KEY)")
        c.executemany("INSERT INTO selected_threads VALUES(?)", [(x,) for x in keep_ids])
        if is_state:
            c.execute("DELETE FROM threads WHERE id NOT IN (SELECT id FROM selected_threads)")
            for t in STATE_THREAD_TABLES:
                c.execute(f"DELETE FROM {qi(t)} WHERE thread_id NOT IN (SELECT id FROM selected_threads)")
            c.execute("DELETE FROM thread_spawn_edges WHERE parent_thread_id NOT IN (SELECT id FROM selected_threads) OR child_thread_id NOT IN (SELECT id FROM selected_threads)")
            c.execute("CREATE TEMP TABLE selected_projects(id TEXT PRIMARY KEY)")
            c.executemany("INSERT INTO selected_projects VALUES(?)", [(x,) for x in project_ids])
            c.execute("INSERT OR IGNORE INTO selected_projects SELECT project_id FROM threads WHERE project_id IS NOT NULL")
            c.execute("DELETE FROM projects WHERE id NOT IN (SELECT id FROM selected_projects)")
            c.execute("DELETE FROM project_roots WHERE project_id NOT IN (SELECT id FROM selected_projects)")
            c.execute("DELETE FROM thread_sections WHERE id NOT IN (SELECT thread_section_id FROM threads WHERE thread_section_id IS NOT NULL)")
            for t in ("remote_control_enrollments", "external_agent_config_imports", "backfill_state",
                      "rollout_migration_state", "rollout_migration_skipped_rollouts", "project_idempotency_keys"):
                c.execute(f"DELETE FROM {qi(t)}")
        else:
            for t in HISTORY_TABLES-{"_sqlx_migrations"}:
                c.execute(f"DELETE FROM {qi(t)} WHERE thread_id NOT IN (SELECT id FROM selected_threads)")
        if "sqlite_sequence" in tables(c):
            # Remove obsolete counters from the exported copy only. Never touch
            # the source or an existing destination's bookkeeping rows.
            c.execute("DELETE FROM sqlite_sequence WHERE name NOT IN (SELECT name FROM sqlite_master WHERE type='table')")
        c.commit()
        c.execute("VACUUM")
        check_db(c)


def filtered_global(g, plan):
    ids = set(plan["thread_ids"])
    aliases = {a for p in plan["projects"] for a in p["aliases"]}
    result = {}
    for key in ("thread-project-assignments", "thread-workspace-root-hints", "thread-writable-roots",
                "thread-projectless-output-directories", "electron-thread-read-state-v1"):
        value = g.get(key, {})
        if isinstance(value, dict):
            result[key] = {k: v for k, v in value.items() if k in ids}
    result["local-projects"] = {k: v for k, v in g.get("local-projects", {}).items() if k in aliases}
    result["pinned-thread-ids"] = [x for x in g.get("pinned-thread-ids", []) if x in ids]
    titles = g.get("thread-titles", {})
    result["thread-titles"] = {"titles": {k: v for k, v in titles.get("titles", {}).items() if k in ids},
                               "order": [x for x in titles.get("order", []) if x in ids]}
    return result


def hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_copy(source, dest):
    before = source.stat()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        fail("File changed during snapshot; stop Codex and retry: " + str(source))


def asset_references(value, home):
    if isinstance(value, dict):
        for key, v in value.items():
            if key in PATH_KEYS and isinstance(v, str):
                text = v[7:] if v.startswith("file://") else v
                p = Path(text)
                if p.is_absolute() and under(p, home):
                    rel = p.relative_to(home)
                    if rel.parts and rel.parts[0] in ASSET_DIRS:
                        yield str(p)
            yield from asset_references(v, home)
    elif isinstance(value, list):
        for v in value:
            yield from asset_references(v, home)


def counts(path):
    with contextlib.closing(ro(path)) as c:
        return {t: c.execute(f"SELECT count(*) FROM {qi(t)}").fetchone()[0] for t in sorted(tables(c))}


def export_main():
    p = argparse.ArgumentParser(description="Export selected local Codex projects or threads without copying source code.",
        epilog="Start with --list-projects or --list-threads, then preview with --project NAME --dry-run. See README.md for the migration workflow.")
    home_arg(p)
    p.add_argument("--project", action="append", help="Exact project name, ID, or registered root; repeatable")
    p.add_argument("--thread", action="append", help="Exact thread ID; repeatable")
    p.add_argument("--cwd", action="append", help="Select threads under this working-directory prefix; repeatable")
    p.add_argument("--list-projects", action="store_true", help="List local project IDs, names, roots and thread counts; no export")
    p.add_argument("--list-threads", action="store_true", help="List stored thread IDs, titles and active/archived status; no export")
    p.add_argument("--output", type=Path, help="New bundle directory; must not already exist")
    p.add_argument("--dry-run", action="store_true", help="Preview the selection and dependencies; create no bundle")
    p.add_argument("--codex-stopped", action="store_true", help="Acknowledge that source clients are stopped (export does not automatically check)")
    p.add_argument("--snapshot-live", action="store_true", help="Allow online snapshots; cross-database consistency is not guaranteed")
    if len(sys.argv) == 1:
        p.print_help()
        return
    a = p.parse_args()
    home = a.codex_home.expanduser().resolve()
    source_paths(home)
    g = read_json(home/GLOBAL)
    with contextlib.closing(ro(home/STATE)) as c:
        validate_schema(c, STATE_TABLES, STATE)
        plan = selection(home, c, g, a)
    if a.list_projects or a.list_threads:
        report("projects" if a.list_projects else "threads", plan, a, home)
        return
    summary = {"primary_threads": len(plan["primary_ids"]), "dependency_threads": len(plan["dependency_ids"]),
               "projects": [{"id": x["id"], "name": x["name"], "roots": x["roots"]} for x in plan["projects"]],
               "thread_ids": plan["thread_ids"], "session_bytes": sum(Path(x["rollout_path"]).stat().st_size for x in plan["threads"].values())}
    if a.dry_run:
        report("export-preview", summary, a, home)
        return
    if not (a.codex_stopped or a.snapshot_live):
        fail("Quit all Codex app/CLI/IDE clients, then supply --codex-stopped. Use --dry-run to preview without copying.")
    if not a.output:
        fail("--output is required for export")
    out = a.output.expanduser().absolute()
    if out.exists() or out.is_symlink():
        fail("Output already exists: " + str(out))
    if under(out.resolve(), home):
        fail("Bundle output must be outside CODEX_HOME")
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".codex-export-", dir=out.parent))
    try:
        for name in (STATE, HISTORY):
            backup(home/name, stage/name)
        # Re-evaluate against the state snapshot, not an earlier live selection.
        with contextlib.closing(ro(stage/STATE)) as c:
            plan = selection(home, c, g, a)
        prune_database(stage/STATE, plan["thread_ids"], [x["id"] for x in plan["projects"]], True)
        prune_database(stage/HISTORY, plan["thread_ids"], [], False)
        signatures = {}
        for name in (STATE, HISTORY):
            with contextlib.closing(ro(stage/name)) as c:
                signatures[name] = schema_signature(c)
        sessions = {}
        assets = set()
        for tid, row in plan["threads"].items():
            src = Path(row["rollout_path"])
            if not src.is_absolute():
                src = home/src
            relative = Path("sessions")/tid/src.name
            stable_copy(src, stage/relative)
            sessions[tid] = relative.as_posix()
            # Scan one JSONL event at a time. No whole-thread size limit.
            with (stage/relative).open("rb") as f:
                for line in f:
                    if line.strip():
                        assets.update(asset_references(json.loads(line), home))
        with contextlib.closing(ro(stage/HISTORY)) as c:
            for table in ("thread_items", "thread_realtime_items"):
                for row in c.execute(f"SELECT item_json FROM {table}"):
                    assets.update(asset_references(json.loads(row[0]), home))
            # Offset preservation is required even for a history-only archive.
            for row in c.execute("SELECT thread_id,next_rollout_byte_offset FROM thread_history_projection_state"):
                if row[1] > (stage/sessions[row[0]]).stat().st_size:
                    fail("History projection extends past copied session: " + row[0])
        copied_assets = {}
        asset_directories = {}
        missing_assets = []
        for src_text in sorted(assets):
            src = Path(src_text)
            if src.is_file() and under(src.resolve(), home):
                rel = Path("assets")/src.relative_to(home)
                stable_copy(src, stage/rel)
                copied_assets[src_text] = rel.as_posix()
            elif src.is_dir() and under(src.resolve(), home):
                members = [x for x in src.rglob("*") if x.is_file() and under(x.resolve(), home)]
                if not members:
                    missing_assets.append(src_text + " (empty directory)")
                    continue
                asset_directories[src_text] = (Path("assets")/src.relative_to(home)).as_posix()
                for member in members:
                    rel = Path("assets")/member.relative_to(home)
                    stable_copy(member, stage/rel)
                    copied_assets[str(member)] = rel.as_posix()
            else:
                missing_assets.append(src_text)
        dump(stage/"global-state.json", filtered_global(g, plan))
        # Fingerprint every payload file, including databases and assets.
        files = {x.relative_to(stage).as_posix(): {"bytes": x.stat().st_size, "sha256": hash_file(x)}
                 for x in sorted(stage.rglob("*")) if x.is_file()}
        manifest = {"format": FORMAT, "bundle_id": str(uuid.uuid4()),
                    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "source_home": str(home), "snapshot": "offline" if a.codex_stopped else "online-nonatomic",
                    "schema_signatures": signatures, "files": files, "sessions": sessions,
                    "assets": copied_assets, "asset_directories": asset_directories, "missing_assets": missing_assets,
                    "primary_ids": plan["primary_ids"], "dependency_ids": plan["dependency_ids"],
                    "projects": plan["projects"], "owners": plan["owners"],
                    "counts": {name: counts(stage/name) for name in (STATE, HISTORY)}}
        dump(stage/"manifest.json", manifest)
        stage.rename(out)
        report("export", {"bundle": str(out), "thread_count": len(sessions), "dependency_count": len(plan["dependency_ids"]),
                          "bytes": sum(x["bytes"] for x in files.values()), "missing_assets": missing_assets,
                          "snapshot": manifest["snapshot"]}, a, home)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def safe_bundle_path(bundle, relative):
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        fail("Unsafe bundle path: " + relative)
    p = bundle/rel
    if not under(p.resolve(), bundle.resolve()) or any(x.is_symlink() for x in [p, *p.parents] if under(x, bundle)):
        fail("Bundle symlinks/path escapes are not supported: " + relative)
    return p


def safe_thread_id(tid):
    if not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", tid):
        fail("Unsafe thread ID in storage/bundle")


def verify_bundle(bundle):
    if not bundle.is_dir():
        fail("Bundle directory does not exist: " + str(bundle))
    if not (bundle/"manifest.json").is_file():
        fail("manifest.json missing from bundle directory: " + str(bundle))
    if (bundle/"manifest.json").is_symlink():
        fail("Manifest must not be a symlink")
    m = read_json(bundle/"manifest.json")
    if m.get("format") != FORMAT:
        fail("Not a supported Codex history bundle")
    try:
        uuid.UUID(m["bundle_id"])
    except (KeyError, ValueError):
        fail("Invalid bundle ID")
    for name in (STATE, HISTORY, "global-state.json"):
        if name not in m.get("files", {}):
            fail("Missing manifest file: " + name)
    for rel, info in m["files"].items():
        p = safe_bundle_path(bundle, rel)
        if not p.is_file() or p.stat().st_size != info["bytes"] or hash_file(p) != info["sha256"]:
            fail("Bundle checksum/size mismatch: " + rel)
    for name, expected in ((STATE, STATE_TABLES), (HISTORY, HISTORY_TABLES)):
        with contextlib.closing(ro(bundle/name)) as c:
            validate_schema(c, expected, name)
            if schema_signature(c) != m["schema_signatures"][name]:
                fail("Bundle schema signature mismatch: " + name)
            check_db(c)
    with contextlib.closing(ro(bundle/STATE)) as c:
        tids = {r[0] for r in c.execute("SELECT id FROM threads")}
    if tids != set(m["sessions"]):
        fail("Session manifest and thread database do not match")
    for tid, rel in m["sessions"].items():
        safe_thread_id(tid)
        if not Path(rel).parts or Path(rel).parts[0] != "sessions":
            fail("Session file outside bundle session storage")
        if rel not in m["files"] or first_metadata(safe_bundle_path(bundle, rel)).get("id") != tid:
            fail("Invalid session mapping: " + tid)
    for old, rel in m["assets"].items():
        if rel not in m["files"]:
            fail("Unverified asset: " + old)
    for old, rel in m.get("asset_directories", {}).items():
        safe_bundle_path(bundle, rel)
        if not any(under(path, rel) for path in m["assets"].values()):
            fail("Unverified asset directory: " + old)
    with contextlib.closing(ro(bundle/HISTORY)) as c:
        for table in HISTORY_TABLES-{"_sqlx_migrations"}:
            if any(r[0] not in tids for r in c.execute(f"SELECT DISTINCT thread_id FROM {qi(table)}")):
                fail("Unselected thread in history: " + table)
        for tid, offset in c.execute("SELECT thread_id,next_rollout_byte_offset FROM thread_history_projection_state"):
            if offset > m["files"][m["sessions"][tid]]["bytes"]:
                fail("Invalid history offset: " + tid)
    return m


def path_maps(values):
    result = []
    for value in values or []:
        if "=" not in value:
            fail("Path map must be OLD=NEW")
        old, new = value.split("=", 1)
        if not Path(old).is_absolute() or not Path(new).is_absolute() or not old or not new:
            fail("Both sides of path maps must be absolute")
        result.append((os.path.normpath(old), os.path.normpath(new)))
    return sorted(result, key=lambda x: len(x[0]), reverse=True)


def mapped(value, mappings):
    normalized = os.path.normpath(value)
    for old, new in mappings:
        if under(normalized, old):
            return new + normalized[len(old):]
    return value


def remap_asset_fields(value, assets):
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if k in PATH_KEYS and isinstance(v, str):
                prefix = "file://" if v.startswith("file://") else ""
                old = v[len(prefix):]
                result[k] = prefix + assets[old] if old in assets else remap_asset_fields(v, assets)
            else:
                result[k] = remap_asset_fields(v, assets)
        return result
    if isinstance(value, list):
        return [remap_asset_fields(x, assets) for x in value]
    return value


def insert_rows(dest, source, table, predicate=None, transform=None, conflict=False):
    names = [r[1] for r in source.execute(f"PRAGMA table_info({qi(table)})")]
    verb = "INSERT OR IGNORE" if conflict else "INSERT"
    sql = f"{verb} INTO {qi(table)} ({','.join(qi(x) for x in names)}) VALUES ({','.join('?' for _ in names)})"
    batch = []
    for record in source.execute(f"SELECT * FROM {qi(table)}"):
        row = dict(zip(names, record))
        if predicate and not predicate(row):
            continue
        if transform:
            row = transform(row)
        batch.append(tuple(row[x] for x in names))
        if len(batch) >= 100:
            dest.executemany(sql, batch)
            batch.clear()
    if batch:
        dest.executemany(sql, batch)


def existing_plan(home, bundle, m, mappings, skip):
    for name in (STATE, HISTORY, GLOBAL):
        if (home/name).is_symlink():
            fail("Destination managed files must not be symlinks: " + name)
    present = [(home/name).exists() for name in (STATE, HISTORY)]
    if any(present) and not all(present):
        fail("Destination contains only one history database; initialize/repair Codex before importing")
    existing_ids = set()
    current_projects = []
    sections = {}
    if all(present):
        source_paths(home)
        for name in (STATE, HISTORY):
            with contextlib.closing(ro(home/name)) as c, contextlib.closing(ro(bundle/name)) as src:
                comparison = schema_comparison(src, c)
                if not comparison["compatible"]:
                    changes = ", ".join(x["kind"] + ":" + x["name"] + "(" + x.get("difference", "changed") + ")"
                                        for x in comparison["blocking_differences"][:8])
                    fail(f"Destination {name} schema/migrations differ: {changes}. Run with --diagnose for a read-only comparison. No automatic schema conversion is attempted.")
        with contextlib.closing(ro(home/STATE)) as c:
            existing_ids = {r[0] for r in c.execute("SELECT id FROM threads")}
            current_projects, _ = catalog(home, c, read_json(home/GLOBAL))
            current_projects = list(current_projects.values())
            sections = {r["id"]: dict(r) for r in c.execute("SELECT * FROM thread_sections")}
    incoming = set(m["sessions"])
    conflicts = incoming & existing_ids
    if conflicts and not skip:
        fail(f"{len(conflicts)} thread IDs already exist. Nothing imported. Use --skip-existing to leave existing threads untouched.")
    ids = incoming-conflicts
    project_map = {}
    project_roots = {}
    for project in m["projects"]:
        pid = project["id"]
        roots = [mapped(x, mappings) for x in project["roots"]]
        project_roots[pid] = roots
        same_id = [p for p in current_projects if p["id"] == pid]
        same_roots = [p for p in current_projects if roots and set(p["roots"]) == set(roots)]
        if same_id and set(same_id[0]["roots"]) != set(roots):
            fail(f"Project ID {pid} exists with different roots; use a matching --path-map")
        if len(same_roots) > 1:
            fail("Destination has ambiguous project roots: " + str(roots))
        project_map[pid] = same_id[0]["id"] if same_id else same_roots[0]["id"] if same_roots else pid
    with contextlib.closing(ro(bundle/STATE)) as src:
        for row in src.execute("SELECT * FROM thread_sections"):
            if row["id"] in sections and sections[row["id"]] != dict(row):
                fail("Conflicting sidebar section ID: " + row["id"])
    return ids, conflicts, project_map, project_roots, bool(all(present))


def merge_global(current, incoming, m, ids, project_map, roots, mappings, home):
    # Only selected presentation metadata; never copy enrollment IDs, queues or settings.
    g = json.loads(json.dumps(current))
    local_projects = g.setdefault("local-projects", {})
    assignments = g.setdefault("thread-project-assignments", {})
    for p in m["projects"]:
        pid = project_map[p["id"]]
        if pid not in local_projects:
            local_projects[pid] = {"id": pid, "name": p["name"], "rootPaths": roots[p["id"]],
                                   "createdAt": 0, "updatedAt": 0}
        order = g.setdefault("project-order", [])
        if pid not in order:
            order.append(pid)
        host_map = g.setdefault("app-server-project-id-by-legacy-project-id-by-host", {}).setdefault("local:"+str(home), {})
        host_map[pid] = pid
        for old in p["aliases"]:
            host_map.setdefault(old, pid)
    for tid in ids:
        old_owner = m["owners"].get(tid)
        if old_owner in project_map:
            assignments[tid] = {"projectKind": "local", "projectId": project_map[old_owner]}
    for key in ("thread-workspace-root-hints", "thread-writable-roots", "thread-projectless-output-directories"):
        target = g.setdefault(key, {})
        for tid, value in incoming.get(key, {}).items():
            if tid in ids:
                target[tid] = [mapped(x, mappings) for x in value] if isinstance(value, list) else mapped(value, mappings)
    tt = g.setdefault("thread-titles", {})
    titles = tt.setdefault("titles", {})
    titles.update({k: v for k, v in incoming.get("thread-titles", {}).get("titles", {}).items() if k in ids})
    order = tt.setdefault("order", [])
    order.extend(x for x in incoming.get("thread-titles", {}).get("order", []) if x in ids and x not in order)
    # Imported archived threads are deliberately not pinned or queued.
    return g


def import_main():
    p = argparse.ArgumentParser(description="Import a Codex history bundle without modifying source code. Chat status is preserved by default.",
        epilog="Start with BUNDLE_DIR --dry-run. Quit destination Codex clients before importing with --codex-stopped; running-client checks cannot be bypassed.")
    p.add_argument("bundle", type=Path, help="Exported bundle directory containing manifest.json (not the tool directory)")
    home_arg(p)
    p.add_argument("--path-map", action="append", help="OLD=NEW absolute metadata paths; repeatable")
    p.add_argument("--skip-existing", action="store_true", help="Skip existing thread IDs completely; never overwrite their history")
    p.add_argument("--status", choices=("archived", "preserve"), default="preserve",
                   help="Preserve original active/archived status (default), or archive imported chats")
    p.add_argument("--dry-run", action="store_true", help="Validate and preview imports, skipped IDs and mapped roots; make no changes")
    p.add_argument("--diagnose", action="store_true", help="Print a read-only schema/migration comparison; do not import")
    p.add_argument("--codex-stopped", action="store_true", help="Confirm all clients using destination CODEX_HOME are stopped")
    if len(sys.argv) == 1:
        p.print_help()
        return
    a = p.parse_args()
    home = a.codex_home.expanduser().resolve()
    bundle = a.bundle.expanduser().resolve()
    if under(home, bundle) or under(bundle, home):
        fail("Bundle and destination CODEX_HOME must be separate directories")
    m = verify_bundle(bundle)
    if a.diagnose:
        report("diagnose", compatibility_report(home, bundle), a, home)
        return
    mappings = path_maps(a.path_map)
    ids, skipped, project_map, roots, has_databases = existing_plan(home, bundle, m, mappings, a.skip_existing)
    summary = {"destination": str(home), "import_threads": len(ids), "skip_threads": len(skipped),
               "status_policy": a.status, "sandbox": "read-only", "project_roots": roots,
               "missing_assets": m["missing_assets"], "source_snapshot": m["snapshot"]}
    if a.dry_run or not ids:
        report("import-preview", summary, a, home)
        return
    if not a.codex_stopped:
        fail("Quit all destination Codex app/CLI/IDE clients, then supply --codex-stopped; --dry-run makes no changes.")
    ensure_codex_stopped(home)
    home.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".codex-import-", dir=home))
    backup_dir = home/"transfer-backups"/(datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+str(uuid.uuid4()))
    installed = []
    installing = False
    originals = {}
    file_changes = []
    try:
        if not under(backup_dir.resolve(), home):
            fail("Destination backup directory escapes CODEX_HOME")
        for name in (STATE, HISTORY):
            if has_databases:
                backup(home/name, stage/name)
            else:
                backup(bundle/name, stage/name)
                # Empty the copied schemas, preserving migrations and all indexes/triggers.
                with contextlib.closing(sqlite3.connect(stage/name)) as c:
                    for table in tables(c)-{"_sqlx_migrations"}:
                        c.execute(f"DELETE FROM {qi(table)}")
                    c.commit()
                    c.execute("VACUUM")
        # Save pre-import DB snapshots, including WAL content, before merging.
        backup_dir.mkdir(parents=True)
        for name in (STATE, HISTORY):
            if has_databases:
                shutil.copyfile(stage/name, backup_dir/name)
                originals[name] = True
            else:
                originals[name] = False
        if (home/GLOBAL).exists():
            stable_copy(home/GLOBAL, backup_dir/GLOBAL)
            originals[GLOBAL] = True
        else:
            originals[GLOBAL] = False
        destination_sessions = {}
        with contextlib.closing(ro(bundle/STATE)) as src:
            source_archived = {r["id"]: bool(r["archived"]) for r in src.execute("SELECT id,archived FROM threads")}
        for tid in sorted(ids):
            bucket = "archived_sessions" if a.status == "archived" or source_archived[tid] else "sessions"
            rel = Path(bucket)/("codex-transfer-"+m["bundle_id"])/tid/Path(m["sessions"][tid]).name
            target = home/rel
            if target.exists():
                fail("Session target already exists: " + str(target))
            stable_copy(safe_bundle_path(bundle, m["sessions"][tid]), stage/rel)
            destination_sessions[tid] = str(target)
            file_changes.append(rel)
        assets = {}
        for old, rel in m["assets"].items():
            # Bundle path, not a supplied source absolute path, determines the write location.
            suffix = Path(rel).relative_to("assets")
            if not suffix.parts or suffix.parts[0] not in ASSET_DIRS:
                fail("Asset outside allowed storage directories")
            target_rel = Path("attachments")/("codex-transfer-"+m["bundle_id"])/suffix
            if (home/target_rel).exists():
                if hash_file(home/target_rel) != m["files"][rel]["sha256"]:
                    fail("Conflicting asset target")
            else:
                stable_copy(safe_bundle_path(bundle, rel), stage/target_rel)
                file_changes.append(target_rel)
            assets[old] = str(home/target_rel)
        for old, rel in m.get("asset_directories", {}).items():
            suffix = Path(rel).relative_to("assets")
            if not suffix.parts or suffix.parts[0] not in ASSET_DIRS:
                fail("Asset directory outside allowed storage directories")
            assets[old] = str(home/"attachments"/("codex-transfer-"+m["bundle_id"])/suffix)
        timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        with contextlib.closing(ro(bundle/STATE)) as src, contextlib.closing(sqlite3.connect(stage/STATE)) as dst:
            dst.execute("PRAGMA foreign_keys=OFF")
            insert_rows(dst, src, "thread_sections", conflict=True)
            def project_transform(r):
                r["id"] = project_map.get(r["id"], r["id"])
                return r
            insert_rows(dst, src, "projects", transform=project_transform, conflict=True)
            # A legacy-only project may not have a native source row.
            for project in m["projects"]:
                pid = project_map[project["id"]]
                if not dst.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                    dst.execute("INSERT INTO projects(id,name,metadata,position,created_at_ms,updated_at_ms) VALUES(?,?,?,0,?,?)",
                                (pid, project["name"], "{}", timestamp*1000, timestamp*1000))
                if not dst.execute("SELECT 1 FROM project_roots WHERE project_id=?", (pid,)).fetchone():
                    dst.executemany("INSERT INTO project_roots VALUES(?,?,?)", [(pid, n, x) for n, x in enumerate(roots[project["id"]])])
            def thread_transform(r):
                tid = r["id"]
                r["rollout_path"] = destination_sessions[tid]
                r["cwd"] = mapped(r["cwd"], mappings)
                if a.status == "archived":
                    r["archived"] = 1
                    r["archived_at"] = timestamp
                r["sandbox_policy"] = json.dumps({"type": "read-only"}, separators=(",", ":"))
                r["approval_mode"] = "on-request"
                r["is_pinned"] = 0
                r["memory_mode"] = "disabled"
                r["daybreak_enabled"] = 0
                old_owner = m["owners"].get(tid) or r.get("project_id")
                r["project_id"] = project_map.get(old_owner)
                return r
            insert_rows(dst, src, "threads", predicate=lambda r: r["id"] in ids, transform=thread_transform)
            for table in STATE_THREAD_TABLES:
                # Worktree attachments remain in the bundle, not registered as live destination worktrees.
                insert_rows(dst, src, table, predicate=lambda r: r["thread_id"] in ids and
                            (table != "thread_attachments" or r.get("attachment_type") not in ("worktree", "archived_worktree")))
            available = {r[0] for r in dst.execute("SELECT id FROM threads")}
            insert_rows(dst, src, "thread_spawn_edges", predicate=lambda r: r["child_thread_id"] in ids and
                        r["parent_thread_id"] in available, conflict=True)
            dst.commit()
            check_db(dst)
        with contextlib.closing(ro(bundle/HISTORY)) as src, contextlib.closing(sqlite3.connect(stage/HISTORY)) as dst:
            def history_transform(r):
                if "item_json" in r and assets:
                    r["item_json"] = json.dumps(remap_asset_fields(json.loads(r["item_json"]), assets), ensure_ascii=False, separators=(",", ":"))
                return r
            for table in sorted(HISTORY_TABLES-{"_sqlx_migrations"}):
                insert_rows(dst, src, table, predicate=lambda r: r["thread_id"] in ids, transform=history_transform)
            dst.commit()
            check_db(dst)
        g = merge_global(read_json(home/GLOBAL), read_json(bundle/"global-state.json"), m, ids, project_map, roots, mappings, home)
        dump(stage/GLOBAL, g)
        receipt = dict(summary, bundle_id=m["bundle_id"], backup=str(backup_dir),
                       new_files=[str(x) for x in file_changes], originals=originals,
                       new_thread_ids=sorted(ids), status="prepared")
        dump(backup_dir/"receipt.json", receipt)
        # All logical checks happen before installation. Ordinary exceptions restore the old files.
        ensure_codex_stopped(home)
        installing = True
        for rel in file_changes:
            target = home/rel
            if not under(target.resolve(), home) or target.exists():
                fail("Unsafe or occupied destination path: " + str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage/rel, target)
            installed.append(rel)
        for name in (STATE, HISTORY, GLOBAL):
            # SQLite backups include WAL content. Old sidecars must not attach to replaced DB files.
            if name.endswith(".sqlite"):
                for suffix in ("-wal", "-shm"):
                    side = home/(name+suffix)
                    if side.exists():
                        os.replace(side, backup_dir/(name+suffix+".original-sidecar"))
            os.replace(stage/name, home/name)
            installed.append(Path(name))
        receipt["status"] = "complete"
        dump(backup_dir/"receipt.json", receipt)
        summary["backup"] = str(backup_dir)
        summary["new_thread_ids"] = sorted(ids)
        report("import", summary, a, home)
    except BaseException:
        # Restored snapshots already incorporate the previous WAL; don't restore old sidecars.
        if installing:
            for rel in reversed(installed):
                if str(rel) not in originals:
                    (home/rel).unlink(missing_ok=True)
            # Restore every managed DB snapshot, including a DB whose WAL was moved
            # before its replacement failed. Never reattach pre-backup WAL sidecars.
            for name in (STATE, HISTORY, GLOBAL):
                if name.endswith(".sqlite"):
                    for suffix in ("-wal", "-shm"):
                        (home/(name+suffix)).unlink(missing_ok=True)
                if originals[name]:
                    shutil.copyfile(backup_dir/name, home/name)
                else:
                    (home/name).unlink(missing_ok=True)
        if backup_dir.exists():
            dump(backup_dir/"failure.json", {"status": "failed-or-rolled-back", "installed_files": [str(x) for x in installed]})
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def run(fn):
    try:
        fn()
    except (TransferError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as e:
        print("ERROR: " + str(e), file=sys.stderr)
        sys.exit(1)
