# ssd-audit — project instructions

Read-only tool that compares two drives and reports missing files, content
conflicts and duplicates. Python 3.10+, standard library only.

## Non-negotiables

**The tool never writes to an audited drive.** No copy, move, delete or rename,
ever. Remediation is emitted as scripts the user reviews and runs themselves. If
a change would have the tool modify a drive, it's the wrong change.

**Generated scripts cannot destroy data on their own.**
- Copy scripts use robocopy without `/MIR`, `/PURGE` or `/MOVE` — they only add.
- Duplicate cleanup is emitted commented out, and quarantines rather than deletes.
- Conflicts are never scripted. Choosing which version survives is the user's call.

`tests/test_remediate.py` enforces this by scanning generated scripts for live
destructive verbs. Don't weaken those tests.

**Drives are identified by volume serial, never by drive letter.** External
drives get remounted under different letters; a profile pinned to a letter would
audit the wrong disk. See `volumes.py`.

**No runtime dependencies.** The tool must run from a USB stick on a machine with
no network. `pytest` is the only dev dependency.

## Architecture

Pipeline: `scanner` → `compare` → (`resolver` → `hashing`, cached via `index`) →
`report` + `remediate` + `history`.

| Module | Responsibility |
|---|---|
| `paths.py` | Case/Unicode folding for match keys, `\\?\` long paths, volume-relative cache keys |
| `volumes.py` | Volume identity via `ctypes` + `GetVolumeInformationW` |
| `scanner.py` | `os.scandir` walk, dir pruning, cruft/case-collision detection |
| `index.py` | SQLite hash cache keyed on `(volume serial, volume-relative path, size, mtime)` |
| `hashing.py` | Two-tier blake2b: quick (size + both 64 KB ends) and full |
| `resolver.py` | Cache lookups on the main thread, reads on a shared pool |
| `compare.py` | Metadata-first bucket classification |
| `dupes.py` | size → quick hash → full hash funnel |
| `report.py` / `remediate.py` / `history.py` | Outputs, scripts, ledger |

## Things that look like bugs but aren't

- **mtime never decides a difference on its own.** A mismatch only promotes a
  pair into the hash queue. exFAT has 2-second granularity, and whole-hour DST
  shifts are classified as artefacts.
- **The cache key is volume-relative, not scan-relative.** `D:\A\notes.txt` and
  `D:\B\notes.txt` are both `notes.txt` relative to their scan roots; keying on
  that let one overwrite the other's digest.
- **Cross-drive duplicates are excluded from copy scripts.** A moved file looks
  identical to a missing one; copying it creates a second copy under a second
  name.
- **`--verify smart` trusts size + mtime.** A file edited in place with an
  unchanged size and timestamp is invisible to it. That's what `--verify full`
  is for, and it's documented rather than fixed — full hashing every run is
  unusable at these drive sizes.

## Privacy

**This repository is public.** Audit output contains real file and folder names
from personal drives. Runs are written to `%LOCALAPPDATA%\ssdaudit\audits\` by
default, and `.gitignore` blocks the artefact names in case `--out` points at
the working tree. Never commit an audit, a manifest, or a real `profiles.json`.

## Tests

```bash
pytest -q
```

Everything runs against synthetic trees built in `tests/conftest.py` — no real
drive is read. The fixture covers each classification bucket deliberately,
including DST drift, same-size-different-content, and NFC/NFD names. Add to it
rather than building ad-hoc trees in individual tests.
