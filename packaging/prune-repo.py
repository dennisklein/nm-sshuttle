#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Delete all but the newest N versions of each package in an RPM directory.

Only the newest release of each of the N newest versions is kept, so a
release bump does not push the previous version out. The newest version of
the previous major.minor line is kept as well, so that a pin to the last
release before a minor change keeps resolving.

Usage: prune-repo.py --keep N DIR
"""

import argparse
import functools
import os
import re
import sys

import rpm


def header(ts, path):
    with open(path, "rb") as f:
        return ts.hdrFromFdno(f.fileno())


def evr(hdr):
    epoch = hdr[rpm.RPMTAG_EPOCH]
    return (
        "0" if epoch is None else str(epoch),
        hdr[rpm.RPMTAG_VERSION],
        hdr[rpm.RPMTAG_RELEASE],
    )


def line(version):
    """The major.minor line of a version."""
    return tuple(version.split(".")[:2])


def doomed(versions, keep):
    """Return the paths to delete from versions, (evr, path) sorted newest first."""
    wanted = []
    for (epoch, version, _), _path in versions:
        if (epoch, version) not in wanted and len(wanted) < keep:
            wanted.append((epoch, version))
    newest_line = line(versions[0][0][1])
    previous = next(((e, v) for (e, v, _), _path in versions if line(v) != newest_line), None)
    if previous is not None and previous not in wanted:
        wanted.append(previous)
    kept, paths = set(), []
    for (epoch, version, _), path in versions:
        # Sorted newest first: the first build of a version is its newest.
        if (epoch, version) in wanted and (epoch, version) not in kept:
            kept.add((epoch, version))
            continue
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=int, required=True)
    parser.add_argument("dir")
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep must be at least 1")

    ts = rpm.TransactionSet()
    # Only names and versions are read; signatures are checked elsewhere.
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)

    by_name = {}
    for entry in sorted(os.listdir(args.dir)):
        if not entry.endswith(".rpm"):
            continue
        path = os.path.join(args.dir, entry)
        hdr = header(ts, path)
        # The source directory holds one SRPM per Fedora release, and each
        # release keeps its own versions.
        dist = re.search(r"\.fc\d+$", hdr[rpm.RPMTAG_RELEASE])
        key = (
            hdr[rpm.RPMTAG_NAME],
            hdr[rpm.RPMTAG_ARCH],
            bool(hdr[rpm.RPMTAG_SOURCEPACKAGE]),
            dist and dist.group(),
        )
        by_name.setdefault(key, []).append((evr(hdr), path))

    cmp = functools.cmp_to_key(lambda a, b: rpm.labelCompare(a[0], b[0]))
    for versions in by_name.values():
        versions.sort(key=cmp, reverse=True)
        for path in doomed(versions, args.keep):
            print(f"pruning {path}", file=sys.stderr)
            os.remove(path)


if __name__ == "__main__":
    main()
