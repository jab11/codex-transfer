# Codex Transfer

Export selected local Codex projects or threads, then import their conversation
history on another computer. Keep the rest of each machine's history in place.
Transfer a whole project, several projects, individual threads, or a directory
subtree. Large sessions are streamed: there is no 10 MB per-thread limit.
If you already imported history with incorrect folders, use
`remap-codex-paths.py` to repair the stored project and thread paths.

This is an **unofficial, schema-specific migration tool**. It works directly with
local Codex storage, not an OpenAI service or supported interchange API. Preview
every import, keep the automatically created backups, and verify the result in
the destination app. Unknown database versions/layouts are rejected.

## Requirements and installation

- Python 3.9 or newer; no Python packages to install.
- macOS or Linux for modifying imports. `ps` is required; existing destinations
  also require `lsof` (included with macOS; install it on Linux if absent).
- Compatible Codex SQLite layouts on both computers: `state_5.sqlite` and
  `thread_history_1.sqlite`. Matching Codex versions are a useful starting point,
  but the stored schemas and migration records determine compatibility.
- Enough free disk space for database snapshots, staged files, and import backups.

Clone this repository on each computer, or download it as a ZIP:

```bash
git clone https://github.com/jab11/codex-transfer.git
cd codex-transfer
python3 codex-export.py --help
python3 codex-import.py --help
python3 remap-codex-paths.py --help
```

Keep `transfer.py` beside all three scripts. Export/import entry scripts show
help when invoked with no arguments and report their version with `--version`.

## Quick start: migrate one project

On the **source computer**, list local projects and preview your selection:

```bash
python3 codex-export.py --list-projects
python3 codex-export.py --project 'My Project' --dry-run
```

Quit all source Codex desktop, CLI and IDE clients, then export to a new directory:

```bash
python3 codex-export.py --project 'My Project' \
  --output "$HOME/codex-bundles/my-project" --codex-stopped
```

Copy that **entire bundle directory** to the destination. Also copy your source
code separately if you want to continue development; the bundle contains history
and project metadata, not your repository.

On the **destination computer**, install Codex and sign in normally. Preview the
bundle, mapping the project's old location if its source folder moved:

```bash
python3 codex-import.py "$HOME/codex-bundles/my-project" \
  --path-map '/Users/old/workspace/project=/Users/new/Projects/project' --dry-run
```

Check the reported paths and counts. Quit all destination Codex clients, then run
the same command with `--codex-stopped` in place of `--dry-run`:

```bash
python3 codex-import.py "$HOME/codex-bundles/my-project" \
  --path-map '/Users/old/workspace/project=/Users/new/Projects/project' --codex-stopped
```

Omit `--path-map` if the absolute source folder location is unchanged. Reopen
Codex after the import and inspect your chats. The importer creates the local
project registration, or reuses an existing one with matching roots; you do not
need to create the project manually. It does not create or access the source
folder itself, so history can be imported before you copy the code.

## Output and scripting

Export and import commands print readable summaries by default: counts, paths, status policy,
missing assets, backup locations, and next steps. Add `--json` for the original
machine-readable output, with no explanatory prose on stdout:

```bash
python3 codex-export.py --list-projects --json
python3 codex-export.py --project 'My Project' --dry-run --json
python3 codex-import.py "$HOME/codex-bundles/my-project" --dry-run --json
python3 codex-import.py "$HOME/codex-bundles/my-project" --diagnose --json
```

Successful commands exit with status 0. Operational errors exit with status 1
and an `ERROR:` message on stderr; invalid arguments exit with status 2.
`--diagnose` exits with status 0 when it successfully produces a comparison,
even if it finds incompatible schemas. Scripts should inspect each database's
`compatible` field rather than treating that exit status as approval to import.

As of v1.4.0, scripts that parsed the default JSON output must add `--json`.
The bundle format is unchanged; earlier bundles do not need to be re-exported.
The folder-remapping helper prints its preview or repair receipt as JSON.

## What the tools change

The tools copy SQLite records, session files and selected app metadata. They never
invoke Codex, Git, SSH, or a model, and never check out a branch,
apply a patch, or copy/write source repositories. Project path mapping updates
stored metadata; it does not move your code. Import uses read-only `ps` and `lsof`
commands to check for running clients; neither command runs through a shell.

**Import preserves the original active/archived status by default.** Imported
threads start with a stored read-only sandbox policy, no pins, memories disabled,
and Daybreak disabled. Existing destination threads and their settings are left
alone. Queued messages, automations, active goals, credentials, machine enrollment,
plugins and global permissions are not exported/imported. You can change a
thread's permissions later when you deliberately resume development.

## 1. Discover and preview on the old computer

Run these commands from the directory containing the scripts:

```bash
python3 codex-export.py --list-projects
python3 codex-export.py --list-threads
python3 codex-export.py --project 'My Project' --dry-run
```

`--project` accepts an exact project name, registered root path, or ID printed by
`--list-projects`. Ambiguous names are rejected. `--cwd` selects a working-directory
subtree, and `--thread` accepts an exact thread ID. Selectors are repeatable and
their results are combined:

```bash
python3 codex-export.py \
  --project 'Project A' \
  --project 'Project B' \
  --thread THREAD_ID \
  --dry-run
```

Project selection accounts for both native SQLite project IDs and older sidebar
associations in `.codex-global-state.json`. Unassigned threads are matched to the
most specific registered root, to avoid accidentally including nested projects.
Known child threads and required fork/root/parent history are included. The
preview reports added dependencies; a dependency can belong to another project.
Missing required history causes an error instead of a silently incomplete export.

## 2. Export

Quit all Codex desktop, CLI, and IDE clients using the source Codex home. Then run:

```bash
python3 codex-export.py \
  --project 'My Project' \
  --output /absolute/path/my-project-bundle \
  --codex-stopped
```

Or export individual threads:

```bash
python3 codex-export.py \
  --thread THREAD_ID_1 \
  --thread THREAD_ID_2 \
  --output /absolute/path/thread-bundle \
  --codex-stopped
```

The output directory must be new and outside `CODEX_HOME`. The default source home
is the existing `CODEX_HOME` environment value, or `~/.codex`. Override it with
`--codex-home /absolute/path/to/.codex`.

For export, `--codex-stopped` is your acknowledgement, not automatic process detection. Separate
SQLite backups cannot provide a single consistent snapshot while different Codex
processes are modifying databases and session files. `--snapshot-live` is an
explicit diagnostic alternative: it uses read-only SQLite online backups, checks
for session-file changes during each copy, and marks the bundle
`online-nonatomic`. Use a stopped source for your final migration.

The bundle includes:

- Filtered `state_5.sqlite` and `thread_history_1.sqlite` with original schemas,
  indexes, triggers and migration records. Filtering is followed by `VACUUM` so
  deleted records are not left in free database pages.
- Byte-for-byte selected session files, retaining their original basenames.
- Selected project/sidebar associations, without the full global state.
- Structured file references under `.codex/attachments`, `generated_images` and
  `visualizations`, including referenced directories. Missing referenced assets
  are reported. External files and paths embedded only in prose are not copied.
- A manifest with selection/dependencies, row counts, schema signatures, sizes,
  and SHA-256 hashes.

Session files are streamed, and history rows are inserted in batches. There is no
10 MB per-thread limit. Individual JSON events/items must still fit in memory;
SQLite's own record limits and available disk space apply. Export temporarily
backs up both full databases before pruning them.

Copy the complete bundle directory and the tool directory to the new computer.
Treat the bundle as private: it contains conversation text, tool outputs and
historical instructions. Compression or encryption can be handled separately.
Excluded credential/configuration files do not guarantee that a conversation is
secret-free: prompts, tool outputs, and copied assets may contain credentials or
other sensitive data. Never commit a bundle to this public repository.

## 3. Preview and import on the new computer

Install a compatible Codex version and sign in normally. Close its desktop, CLI
and IDE clients before importing. If the destination already has history, the
tools require compatible SQLite layouts and matching migration records and reject
incompatible schemas rather than guessing how to convert them. An empty destination
can receive the bundle's database schemas directly.

Import also checks automatically, before creating staging/backup files and again
immediately before installing the imported data. With the default `~/.codex` home,
a running ChatGPT/Codex desktop app or `codex` CLI/IDE backend blocks the import.
For all destinations, `lsof` checks whether any process holds the destination's
databases, SQLite sidecars, or global metadata file open. A separate isolated
Codex home is allowed when its managed files are not open in another process.

Keep supplying `--codex-stopped` to acknowledge the offline import, but it cannot
bypass detection. An error identifies detected process names and PIDs. If process
inspection is unavailable, denied, or fails, import is blocked rather than
trusting the flag. Existing destinations require `lsof`; macOS includes it.
Automatic checks currently support macOS and Linux. `--dry-run` and `--diagnose`
remain available while the app is open because they make no changes.

These are process snapshots, not an exclusive lock against other applications.
Keep the clients closed until import finishes: a new client could still start
after the final check. Checks for custom homes rely on open managed files and
cannot identify a client that has only cached that home's metadata in memory.

If compatibility is rejected, get a read-only comparison on the destination:

```bash
python3 codex-import.py /absolute/path/my-project-bundle --diagnose
```

The report shows the effective destination Codex home, migration versions, and
exact blocking differences. Matching application versions alone do not prove the
stored databases have identical migration histories. The importer accepts SQL
quoting/whitespace differences and nonunique performance-index differences when
table definitions, columns, foreign keys, constraints, triggers, and migration
records still match. Changed CHECK/unique constraints, columns, triggers or
migration checksums remain blockers. No destination migrations are run or edited.
Presence of SQLite's internal `sqlite_sequence` bookkeeping table is also allowed
to differ: it can remain after an old AUTOINCREMENT table was dropped. This is
reported under `ignored_bookkeeping_differences`; the importer preserves existing
destination bookkeeping rows. Do not delete that table to resolve compatibility.
Existing bundles remain compatible; re-exporting is not required to use this
comparison. A missing visualization/attachment directory is independent of schema
compatibility.

First preview, supplying path changes if necessary:

```bash
python3 codex-import.py /absolute/path/my-project-bundle \
  --path-map '/Users/old/workspace/project=/Users/new/Projects/project' \
  --dry-run
```

Then import:

```bash
python3 codex-import.py /absolute/path/my-project-bundle \
  --path-map '/Users/old/workspace/project=/Users/new/Projects/project' \
  --codex-stopped
```

Repeat `--path-map OLD=NEW` for multiple projects. Both sides must be absolute.
Mappings use directory boundaries and the longest prefix first. They update
thread working directories, project roots, and selected sidebar path metadata.
The session files and historical message text stay unchanged to preserve byte
offsets; old paths can therefore still appear in historical messages and session
headers. Structured attachment paths in SQLite items are adjusted to copied
assets in the destination Codex home.

Import preserves active/archived status. To explicitly archive every imported
thread instead, add `--status archived`.

An existing thread ID causes the import to stop before any destination changes.
Use `--skip-existing` to keep the destination's version of that thread entirely
untouched and import the remaining threads. It does not reconcile divergent
histories. Matching destination project roots are reused; conflicting project IDs
with different roots are rejected. Active/archived worktree registrations remain
in the export bundle but are not registered as destination worktrees.

After a successful import, reopen Codex and inspect several threads before doing
further work. Archived source threads remain in Archived chats. The tools do not
start/resume a thread, replay a command, or send a prompt. Opening/running Codex
afterward is a separate operation and follows the destination's existing setup.

## Repair folders after importing

Use `remap-codex-paths.py` on the computer whose local Codex metadata needs
correcting. It does not require the export bundle or another import. First
inspect the existing registrations:

```bash
python3 codex-export.py --list-projects
```

Then preview the path corrections. No `--apply` means a read-only preview:

```bash
python3 remap-codex-paths.py \
  --path-map '/Users/old/workspace/project=/Users/new/Documents/project'
```

The JSON report lists changed project roots, old/new paths, the number of thread
working directories affected, and whether app metadata will change. Inspect the
report, quit all Codex clients using this home, then apply the same mapping:

```bash
python3 remap-codex-paths.py \
  --path-map '/Users/old/workspace/project=/Users/new/Documents/project' \
  --apply --codex-stopped
```

Repeat `--path-map OLD=NEW` for several projects. Both paths must be absolute;
matching uses directory boundaries and the longest prefix first. All matching
local project roots and thread working directories are updated, including nested
directories. Preserve a project's nested suffix if it belongs below the top of
the source repository. New project roots and affected working directories must
already exist. The helper does not search for folders, create directories, or
choose between registrations with duplicate roots.

Use `--codex-home /absolute/path/to/.codex` for another local storage directory;
otherwise the default is `CODEX_HOME`, or `~/.codex`. Previewing makes no changes.
Applying uses the same stopped-client checks as import, before preparation and
again before installation; `--codex-stopped` cannot bypass them. Keep clients
closed throughout the operation.

The helper updates `project_roots.path`, `threads.cwd`, local sidebar roots,
thread workspace hints, writable-root path metadata, projectless output paths,
and cached local thread workspace state. Thread IDs, project IDs, chat status,
sandbox policies, titles, queued messages, remote-project metadata, source code,
and Git state are retained. It does not merge duplicate projects. Historical
messages and session headers still contain their original paths: the helper
preserves session bytes and verifies hashes of affected session files and the
history database.

Before installation, it backs up `state_5.sqlite` and the original app JSON to
`CODEX_HOME/transfer-backups/path-repair-<timestamp>-<uuid>/`. The returned
`backup` path contains `receipt.json`. Database integrity, foreign keys, schema,
every metadata-table row, and history hashes are checked; changes beyond the
planned path fields cause an error. The helper also checks for metadata changes
that occur during preparation. Ordinary installation exceptions restore the
original metadata snapshots.

If a forced kill or power failure interrupts installation, keep Codex closed and
retain a fresh copy of its current storage before restoring the backup. Restore
only the saved `state_5.sqlite` and `.codex-global-state.json`, removing destination
`state_5.sqlite-wal` and `state_5.sqlite-shm` first. Never reattach saved
`.original-sidecar` files: the snapshot already includes pre-repair WAL contents.
The helper does not modify `thread_history_1.sqlite` or session files, so those
files do not need restoring for a path repair. Restoring old metadata after
subsequent Codex use can discard newer changes; inspect the receipt and backups
before doing so.

## Backups and recovery

Each modifying import first creates
`CODEX_HOME/transfer-backups/<timestamp>-<uuid>/`. It contains standalone snapshots
of pre-import databases, original global JSON when present, and a `receipt.json`
listing new thread files and which original files existed. SQLite snapshots
include the old WAL contents; separately retained `.original-sidecar` files are
diagnostic and must **not** be reattached to the snapshot databases.

Integrity, schema, foreign keys, session headers, offset bounds, and every bundle
file hash are checked. Import stages the database changes before installation.
Normal installation exceptions restore the old snapshots and remove installed
session/asset files. No operation spanning multiple SQLite files and JSON can be
fully atomic against a power failure or a forced process kill.

For manual rollback after such an interruption, keep Codex closed. Use the backup
receipt to restore the original main database/global files (or remove ones that
did not exist), remove the listed newly installed session/asset files, and remove
destination `-wal` and `-shm` sidecars before reopening. Restoring that backup also
discards any destination history added since it was taken, so retain a new backup
before rolling back a machine you have subsequently used.

## Validation and limits

Run the integration suite with:

```bash
python3 -m unittest discover -s tests -v
```

Tests use invented conversation data with the inspected real database schemas.
They cover project filtering with empty `project_id`, dependency closure, changed
paths, existing destination history, preserved/optional archived status, an item
larger than 10 MB, attachment copying, duplicate protection, corruption/schema
rejection, dry runs, and rollback on a simulated database installation failure.
Repository-file hashes are checked throughout.
Six additional folder-remapping tests cover previews, nested paths, preserved
history and unrelated metadata, missing destination folders, duplicate roots,
client reopening before installation, and rollback after an installation failure.
The CI workflow discovers both test files automatically.

These tools target the inspected `state_5.sqlite` and
`thread_history_1.sqlite` layouts. Unknown tables/database versions fail closed.
They are a local data migration implementation, not an official OpenAI import
format. Automated storage checks do not prove that every Codex desktop version
will display/resume imported threads identically; verify the destination app.
The integration suite passes on macOS and Linux with Python 3.9 and 3.13.
Live storage and desktop verification were performed on macOS; migration between
different operating systems and path syntaxes has not been validated.

On October 8, 2026, all 37 integration tests passed, including stopped-client
guards before staging and installation, read-only previews with running clients, compatibility
diagnostics, harmless SQL-format/index differences, and rejection of changed
constraints/migration checksums. An actual 251,040,465-byte
session was also exported and imported into an isolated test Codex home: its
session SHA-256, every history-table row, active status, and mapped working
directory were verified. This diagnostic used the explicitly non-atomic online
snapshot option and never opened the destination in the desktop app. Use an
offline snapshot and verify the real destination before relying on a migration.

## Troubleshooting and common questions

**“Not a supported Codex history bundle” or “manifest.json missing.”**
Pass the directory created by `codex-export.py --output`, not the downloaded tool
directory or its parent. A valid bundle has `manifest.json`, both filtered SQLite
files, `global-state.json`, and its session/asset subdirectories.

**“Destination … schema/migrations differ.”**
Run `codex-import.py BUNDLE_DIR --diagnose`, or add `--json` for full differences.
Even equally versioned apps can have different migration histories. The tool
does not run migrations or edit migration checksums to force compatibility.

**“Destination Codex clients are still running.”**
Quit the listed app/client processes and retry. Closing just a window may leave
the app running. `--codex-stopped` is an acknowledgement, not a bypass. See the
process-check limitations above; keep clients stopped throughout installation.

**“Thread IDs already exist.”**
Use `--skip-existing` only if you want to retain the destination's entire version
of those threads. It imports other IDs and leaves matching ones untouched; it
does not merge two copies that have diverged since export.

**“Destination has ambiguous project roots.”**
Multiple destination project registrations already share the same roots. The
importer refuses to choose one silently. Inspect the catalog with
`codex-export.py --list-projects` on that computer and resolve the duplication
before importing. Do not delete your entire Codex home to resolve this error.

**The count is higher than the number of visible chats.**
Counts include stored child/reviewer threads and parent/fork dependencies, some
of which the desktop app hides. Archived source chats remain in Archived unless
you explicitly change the import status policy. Check the stored catalog with
`--list-threads` rather than assuming a hidden thread was lost.

**Does export mean archive?**
No. Export copies selected history into a bundle without changing the source
chat status. Import preserves active/archived status by default. Use
`--status archived` only to archive all newly imported chats on the destination.

**Does it migrate my account, plugins, source code, or Git worktrees?**
No. Authenticate on the new computer normally and move your source folders
separately. The importer registers projects and history; it does not run models,
replay commands, clone repositories, check out commits, or register destination
worktrees. Worktree attachments remain only in the export bundle.

**Can I keep using both machines afterward?**
The two local copies retain their thread IDs and are not synchronized by this
tool. Re-importing does not reconcile divergent conversations. The tool makes no
claims about account-level cloud features or how future Codex releases might
synchronize data.

## Development

The export/import implementation lives in `transfer.py`; their entry scripts call
its functions. `remap-codex-paths.py` performs offline path repairs and reuses the
shared path mapping, storage checks, and stopped-client guard. `tests/schema.json`
contains SQL definitions only, and the
tests construct invented conversation records in temporary directories. No real
session, bundle, account database, or personal metadata belongs in this repository.

Before opening a pull request, run the integration suite shown above. If a future
Codex schema changes, update and test its validation and migration handling
explicitly rather than relaxing compatibility checks.

## License

MIT; see [LICENSE](LICENSE). This project is not affiliated with or endorsed by
OpenAI.
