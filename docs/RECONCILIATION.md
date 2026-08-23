# Reconciling two drives

How to work through what an audit reports, in the order that minimises risk.

The tool deliberately stops at telling you what's true. This document is the
reasoning it can't do for you.

---

## The order to work in

Do these in sequence. Each step makes the next one smaller and less ambiguous.

1. **Read the summary. Change nothing yet.**
2. **Deal with cruft** — pure noise, removing it shrinks everything else.
3. **Deal with moved files** — otherwise step 4 duplicates them.
4. **Fill the gaps** — copy files that exist on only one drive.
5. **Resolve conflicts** — one at a time, by hand.
6. **Reclaim duplicates** — last, once you know what's genuinely needed.
7. **Re-run the audit** and confirm with `ssdaudit compare-runs -2 latest`.

Steps 3 and 4 are in that order for a reason. Copying first and reorganising
afterwards means you copy files you were about to move anyway.

---

## 1. Missing files

**What it means:** the file exists on one drive and not the other.

Under the peer model these are gaps to fill, never staleness to delete. A file
being on only one drive says nothing about whether it *should* be there.

**How to fix:** run the generated script.

```
sync-left-to-right.cmd     fills gaps on the right drive
sync-right-to-left.cmd     fills gaps on the left drive
```

These are safe by construction: robocopy is invoked without `/MIR`, `/PURGE` or
`/MOVE`, so they can only add files. Each pauses before doing anything.

**Before running, check the held-back section.** Files whose content already
exists on the destination under a different name are listed at the top, commented
out, with the path where they already live. Copying those would create a second
copy rather than fill a gap — see section 3.

**If you actually wanted one drive to mirror the other**, don't use these
scripts. That's a different operation with real deletion semantics, and this tool
doesn't generate it on purpose.

---

## 2. Content conflicts

**What it means:** the same path holds different content on each drive.

**These are never scripted, and never will be.** Copying either direction
destroys the other version. Only you know which one matters.

`conflicts.txt` gives you, for each file: size, modification time, and hash on
both sides.

### Working through them

**Look at the size difference first.** A file that grew is usually the one with
more work in it. A file that shrank to near-zero is usually a failed write or an
interrupted copy — and the *older, larger* version is the one you want.

**Treat "newer" with suspicion.** A newer timestamp means the file was written
more recently, not that it's more correct. Copying a drive with a tool that
doesn't preserve timestamps re-dates everything it touches. The listing says
which side is newer and explicitly warns that newer isn't automatically right.

**For documents**, open both and compare before deciding.

**For anything you can't judge quickly**, keep both:

```cmd
copy "E:\Work\report.docx" "E:\Work\report (left copy).docx"
```

then copy the other version over and reconcile later. Disk is cheaper than lost
work.

**If there are many conflicts with identical timestamps**, suspect the copy tool
rather than the files — something rewrote metadata in bulk. Re-run with
`--verify full` to find out how many differ in *content* versus only in metadata.

---

## 3. Same content, different path

**What it means:** the same bytes exist on both drives, but under different
names or in different folders. Usually you reorganised one drive and not the
other.

This is the category most tools get wrong. A path-based comparison reports the
file as missing from both drives — once under each name — and cheerfully tells
you to copy it in both directions, leaving you with four copies.

**How to fix:** decide which layout you want, then *move* the file on the drive
with the layout you're abandoning:

```cmd
move "F:\Media\archive\clip.mp4" "F:\Media\clip.mp4"
```

Do this before running the copy scripts. Then re-run the audit; the entry
disappears from both the missing lists and this one.

---

## 4. Duplicates within a drive

**What it means:** the same content stored more than once on the same drive.

`review-duplicates.cmd` contains the cleanup, with **every line commented out**.
It *moves* extra copies to `_ssdaudit-quarantine` on the same drive rather than
deleting them — so if the audit was wrong, or you change your mind, the file is
still there.

### Before uncommenting anything

**Check whether the duplication is deliberate.** Photo libraries keep originals
and exports. Project folders keep per-version snapshots. Two identical files
aren't automatically a mistake.

**Check what's referencing them.** A duplicate inside an application's library
folder (Lightroom, iTunes, a game install) may be referenced by path. Moving it
breaks the application, and the file being byte-identical to another elsewhere is
irrelevant.

**The suggested keeper is the shallowest path**, which is a heuristic, not a
judgement. If you'd rather keep the copy in the deeper folder, edit the script.

### Then

Uncomment only the lines you've checked, run it, and use the drive normally for
a week. If nothing broke, delete the quarantine folder.

---

## 5. Cruft

`Thumbs.db`, `.DS_Store`, `._*` and `desktop.ini` are generated by Windows and
macOS and carry nothing of yours. The `._*` files in particular accumulate in
huge numbers on an exFAT drive that's been used on a Mac.

Safe to delete. Check the reported total first — if it's trivial, ignore it.

```cmd
del /s /q "E:\Thumbs.db" "E:\.DS_Store"
```

They come back whenever the drive is browsed again. This is maintenance, not a
fix.

---

## 6. Special cases

### Case collisions

Two files whose names differ only in capitalisation. They can't coexist on
exFAT or NTFS, so if this is reported, the files came from a case-sensitive
filesystem (Linux, or macOS formatted case-sensitively).

**Copying either drive will silently lose one of them.** Rename one before doing
anything else.

### Unicode form differences

The same name written composed (Windows) versus decomposed (macOS). The tool
matches them correctly, so these need no action — but if another sync tool has
been run across these drives, this is very likely where its duplicates came from.

### DST artefacts

Timestamps differing by exactly one hour, with identical content. A filesystem
artefact, not a change. Nothing to do.

### Unverified files

Only appear under `--verify metadata`. Re-run with `--verify smart` to settle
them.

---

## Keeping them in sync afterwards

Re-run the audit after any significant change, and check the trend rather than
the absolute numbers:

```bash
ssdaudit compare --profile photos
ssdaudit history
ssdaudit compare-runs -2 latest
```

`compare-runs` names the specific files whose status changed since last time —
which gaps closed, and which differences are new. Steadily falling counts mean
your process is working. Counts that keep reappearing mean something is writing
to one drive and not the other, and that's worth finding.

Once a quarter, run `--verify full` on the folders you actually care about. It's
slower, but it's the only mode that detects a file that changed without its size
or timestamp changing — silent corruption, or a bad cable.
