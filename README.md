# ssd-audit

Compare two drives and find out what's actually different: which files exist on
only one of them, which share a path but hold different content, and which are
stored twice for no reason.

Built for the situation where you keep two external SSDs, believe they hold
roughly the same things, and have no way to check.

**The tool never writes to the drives it audits.** It produces a report and a
set of scripts for you to review and run yourself.

```
  5,142 files missing from the RIGHT drive (48.2 GB)
    317 files missing from the LEFT drive (2.1 GB)
     12 content conflicts (same path, different content)
     84 duplicate groups (11.4 GB reclaimable)
      6 files present on both drives under different paths
```

---

## Install

Python 3.10+, no dependencies.

```bash
git clone https://github.com/xMPon/ssd-audit.git
cd ssd-audit
pip install -e .
```

## Use it

**Double-click `SSD Audit.cmd`.** A window opens listing every attached drive
with its label, format and capacity. Pick one on each side, choose how
thoroughly to check, press Start.

Nothing is selected for you. The Start button stays disabled until you've
deliberately chosen both sides, because auditing the wrong pair is the mistake
that costs you a long scan.

While it runs you can see which drive is being read, how many files have been
found, and how far the hashing has got. When it finishes, buttons take you
straight to the report or the audit folder.

Want a single file you can carry on the SSD itself and run on a machine with no
Python? Double-click `build-exe.cmd` once to produce `dist\SSD Audit.exe`.

### Terminal

Same wizard, no window:

```bash
ssdaudit run
```

Or drive it directly:

```bash
ssdaudit volumes                                    # attached drives + serials
ssdaudit compare --left E:\ --right F:\             # compare two drives
ssdaudit compare --left E:\ --right F:\ --folder Photos   # one folder only
ssdaudit pick --left E:\ --right F:\                # pick folders, save a profile
ssdaudit compare --profile photos                   # re-run a saved profile
ssdaudit history                                    # every past audit
ssdaudit compare-runs -2 latest                     # what changed since last time
```

Exit codes: `0` drives match · `1` differences found · `2` the audit couldn't run.

---

## How it decides two files are the same

Hashing a 400 GB drive on every run would be unusable, so content is only read
when metadata genuinely can't answer the question.

| Situation | What happens |
|---|---|
| Different size | Different. Proven without reading anything. |
| Same size, same timestamp | Presumed identical. Not read. |
| Same size, timestamp differs | **Ambiguous — the file gets hashed.** |

Only that last row costs I/O, and it's normally a small fraction of a drive.
Every digest is cached against `(volume serial, path, size, mtime)`, so a second
audit of an unchanged drive reads *nothing*.

Three verify levels:

- `--verify metadata` — no content reads at all. Fastest; ambiguous files are
  reported as unverified rather than guessed at.
- `--verify smart` *(default)* — hashes only the ambiguous cases.
- `--verify full` — hashes everything. The only mode that catches a file edited
  in place without its size or timestamp changing, and the one to use if you
  suspect bit rot. The cache makes repeat runs cheap.

## Things it gets right that are easy to get wrong

**Drive letters are never trusted.** Unplug two external drives and reconnect
them in the other order, and `E:` is now the other disk. Profiles record the
volume *serial number* and resolve it to whatever letter the drive currently
holds. If the drive isn't attached, the audit refuses to run rather than
auditing the wrong hardware.

**A moved file isn't a missing file.** If you reorganised `Media/clip.mp4` into
`Media/archive/clip.mp4` on one drive, a path-based comparison calls it missing
and tells you to copy it back — leaving two copies under two names. Cross-drive
duplicate detection catches this, and those files are *held back* from the copy
script with an explanation.

**Timestamps lie in specific ways.** exFAT stores modification times to 2-second
granularity, and FAT-family volumes shift them by exactly one hour across
daylight-saving boundaries. Both are treated as artefacts, not changes —
otherwise half your drive would look modified every spring.

**Filenames aren't bytes.** macOS writes filenames decomposed (NFD), Windows
composed (NFC). The same accented filename written on each platform compares
unequal, so a naive tool copies it again and creates a duplicate. Names are
matched after normalisation, and the divergence is reported separately.

**Junk is counted, not hidden.** `Thumbs.db`, `.DS_Store` and macOS `._*`
files are excluded from the comparison but reported with their reclaimable size,
because on a drive shared between Windows and macOS there are usually thousands.

## Output

Each run writes a timestamped folder to `%LOCALAPPDATA%\ssdaudit\audits\`:

| File | What it's for |
|---|---|
| `summary.md` | The report you read |
| `report.html` | Browsable and filterable, for working through long lists |
| `diff.json` | Complete machine-readable results |
| `manifest-left.jsonl` · `manifest-right.jsonl` | Every file seen, so later runs can diff against it |
| `sync-left-to-right.cmd` · `sync-right-to-left.cmd` | robocopy scripts that fill the gaps |
| `review-duplicates.cmd` | Quarantine moves — **every line commented out** |
| `conflicts.txt` | Files needing a decision. Deliberately not a script. |

`history.jsonl` gets one line per run, which is what `ssdaudit history` and
`ssdaudit compare-runs` read to show whether the drives are converging.

## Fixing what it finds

Short version — the full reasoning is in [docs/RECONCILIATION.md](docs/RECONCILIATION.md):

- **Missing files** → run the generated `sync-*.cmd`. Safe: it only adds files.
- **Conflicts** → never automated. One version is about to be lost and only you
  know which. `conflicts.txt` gives you sizes, timestamps and hashes for both.
- **Duplicates** → `review-duplicates.cmd` *moves* copies to a quarantine folder
  rather than deleting them, and ships with every line disabled.
- **Cruft** → safe to delete, but check the reported size first.

## Safety

- The tool opens files read-only. It never copies, moves, deletes or renames.
- Generated copy scripts use robocopy without `/MIR`, `/PURGE` or `/MOVE` — they
  can only add files, never remove them.
- Deletion is never generated. Duplicate cleanup quarantines instead, so a wrong
  call is recoverable.
- Every script pauses for confirmation before doing anything.

> **Note on privacy:** audit reports contain the full file and folder names from
> your drives. They're written outside this repository by default, and
> `.gitignore` blocks them regardless. Don't paste one into a public issue.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Tests run entirely against synthetic directory trees in a temp folder — no real
drive is touched.

## Licence

MIT — see [LICENSE](LICENSE).
