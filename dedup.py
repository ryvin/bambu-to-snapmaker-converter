#!/usr/bin/env python3
"""
Deduplicate byte-identical output files in a folder.

The converter produces one `.3mf` per model; earlier versions (and lost history)
could leave byte-identical copies under different names — `_U1_v2`, `_U1_v3`,
`name (1)_U1`, etc. This finds groups of files with identical content and keeps
one canonical name per group, removing the redundant copies. Removing a
byte-identical duplicate is lossless: the exact content survives in the kept
file.

Canonical pick (kept file) prefers, in order: a name WITHOUT a `_v<N>` version
suffix, then WITHOUT a ` (N)` copy suffix, then the shortest name, then the
oldest mtime — so `580MM_U1.3mf` is kept over `580MM_U1_v2.3mf`.

CLI:
    python dedup.py FOLDER [--pattern '*.3mf'] [--apply]
Without --apply it is a dry run (reports what it WOULD remove).
"""
import argparse
import fnmatch
import hashlib
import os
import re
import sys

_VERSION_RX = re.compile(r'_v\d+(?=\.[^.]+$)', re.IGNORECASE)   # ..._U1_v2.3mf
_COPY_RX = re.compile(r' \(\d+\)(?=\.[^.]+$)')                   # ...name (1).3mf


def hash_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def _canonical_rank(path):
    """Sort key; the smallest ranks first and is kept. Lower = more canonical."""
    name = os.path.basename(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return (
        1 if _VERSION_RX.search(name) else 0,   # avoid _v2/_v3 versions
        1 if _COPY_RX.search(name) else 0,      # avoid " (1)" copies
        len(name),                              # prefer shorter names
        mtime,                                  # prefer older
    )


def find_duplicate_groups(folder, pattern='*.3mf'):
    """Return a list of groups (each a list of paths with identical content),
    ordered so group[0] is the canonical file to keep. Only groups with >1
    member are returned. A size prefilter means most files are never hashed."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    by_size = {}
    for n in names:
        if not fnmatch.fnmatch(n, pattern):
            continue
        p = os.path.join(folder, n)
        if not os.path.isfile(p):
            continue
        try:
            by_size.setdefault(os.path.getsize(p), []).append(p)
        except OSError:
            pass

    groups = []
    for _size, paths in by_size.items():
        if len(paths) < 2:
            continue  # unique size -> cannot have a content duplicate
        by_hash = {}
        for p in paths:
            try:
                by_hash.setdefault(hash_file(p), []).append(p)
            except OSError:
                pass
        for _h, dupes in by_hash.items():
            if len(dupes) > 1:
                groups.append(sorted(dupes, key=_canonical_rank))
    return groups


def dedup_folder(folder, pattern='*.3mf', apply=False):
    """Find identical-content files and (if apply) remove all but the canonical
    one per group. Returns a report dict with a `pairs` list of
    {'keep': path, 'removed': [paths]} for display and testing."""
    groups = find_duplicate_groups(folder, pattern)
    pairs, freed, removed_count = [], 0, 0
    for g in groups:
        keep, extras = g[0], g[1:]
        gone = []
        for p in extras:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            if apply:
                try:
                    os.remove(p)
                except OSError as e:
                    print(f"WARN: could not remove '{p}': {e}", file=sys.stderr)
                    continue
            gone.append(p)
            freed += size
            removed_count += 1
        pairs.append({'keep': keep, 'removed': gone})
    return {
        'groups': len(groups),
        'pairs': pairs,
        'removed_count': removed_count,
        'bytes_freed': freed,
        'applied': apply,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Remove byte-identical duplicate files, keeping one canonical copy per group.")
    ap.add_argument('folder', help="Folder to deduplicate.")
    ap.add_argument('--pattern', default='*.3mf', help="Filename glob (default: *.3mf).")
    ap.add_argument('--apply', action='store_true', help="Actually delete duplicates (default: dry run).")
    a = ap.parse_args(argv)

    rep = dedup_folder(a.folder, a.pattern, apply=a.apply)
    verb = "Removed" if a.apply else "Would remove"
    print(f"{rep['groups']} duplicate group(s); {verb} {rep['removed_count']} file(s), "
          f"freeing {rep['bytes_freed'] / 1e6:.1f} MB.")
    for pair in rep['pairs']:
        print(f"  keep   {os.path.basename(pair['keep'])}")
        for r in pair['removed']:
            print(f"  {'del  ' if a.apply else 'skip '}  {os.path.basename(r)}")
    if not a.apply and rep['removed_count']:
        print("\n(dry run — re-run with --apply to delete)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
