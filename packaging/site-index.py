#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Write directory listings, like Apache's, into the repository site.

Usage: site-index.py PAGES_URL SITE

Every directory below SITE gets an index.html that lists its entries with
links, times and sizes. The landing page, SITE/index.html, keeps its content
and gets the listing of SITE in place of its <!-- listing --> marker; the
other pages reuse its stylesheet.
"""

import datetime
import html
import os
import re
import sys
import urllib.parse

MARKER = "<!-- listing -->"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Index of {path}</title>
{style}
</head>
<body>
<h1>Index of {path}</h1>
{listing}
</body>
</html>
"""


def newest_mtime(path):
    """The time of the newest file below path, listings aside."""
    times = [os.stat(os.path.join(root, name)).st_mtime
             for root, _, files in os.walk(path) for name in files if name != "index.html"]
    return max(times, default=os.stat(path).st_mtime)


def human(size):
    for unit in ("", "K", "M", "G"):
        if size < 1024 or unit == "G":
            break
        size /= 1024
    if unit == "":
        return str(int(size))
    return f"{size:.1f}{unit}" if size < 10 else f"{size:.0f}{unit}"


def row(href, name, mtime, size):
    when = "" if mtime is None else datetime.datetime.fromtimestamp(
        mtime, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    return (f'<tr><td><a href="{html.escape(href)}">{html.escape(name)}</a></td>'
            f"<td>{when}</td><td class=\"size\">{size}</td></tr>")


def listing(directory, parent):
    rows = []
    if parent:
        rows.append(row("../", "Parent Directory", None, "-"))
    entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name))
    for entry in entries:
        if entry.name == "index.html":
            continue
        quoted = urllib.parse.quote(entry.name)
        if entry.is_dir():
            rows.append(row(quoted + "/", entry.name + "/", newest_mtime(entry.path), "-"))
        else:
            st = entry.stat()
            rows.append(row(quoted, entry.name, st.st_mtime, human(st.st_size)))
    return ('<table class="listing">\n'
            "<thead><tr><th>Name</th><th>Last modified</th>"
            '<th class="size">Size</th></tr></thead>\n<tbody>\n'
            + "\n".join(rows) + "\n</tbody>\n</table>")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip().splitlines()[2])
    base = urllib.parse.urlsplit(sys.argv[1]).path.rstrip("/") + "/"
    site = sys.argv[2]

    landing = os.path.join(site, "index.html")
    with open(landing, encoding="utf-8") as f:
        page = f.read()
    if page.count(MARKER) != 1:
        sys.exit(f"{landing} needs exactly one {MARKER} marker")
    style = re.search(r"<style>.*?</style>", page, re.S)

    for root, dirs, _ in os.walk(site):
        dirs.sort()
        rel = os.path.relpath(root, site)
        if rel == ".":
            continue
        path = base + rel.replace(os.sep, "/") + "/"
        with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
            f.write(PAGE.format(path=html.escape(path), style=style.group(0) if style else "",
                                listing=listing(root, parent=True)))

    with open(landing, "w", encoding="utf-8") as f:
        f.write(page.replace(MARKER, listing(site, parent=False)))


if __name__ == "__main__":
    main()
