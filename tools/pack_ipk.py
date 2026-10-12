#!/usr/bin/env python3
"""Pack the app directory into an ipk that webOS 5 accepts too.

ares-package dates every entry 1970-01-01. webOS 5's pkgverifier extracts
with GNU tar 1.17, which warns of an "implausibly old time stamp", and takes
the warning as a failure: "-5: ipk verified failed" (LG CX, webOS 5.5; see
webosbrew/homebrew-channel-safe-updater#3). A time in the future warns too,
so every timestamp is a fixed past date (which also makes builds
reproducible: the same app, the same ipk and hash).

    tools/pack_ipk.py <app dir> <out dir>
"""

import gzip
import io
import json
import os
import sys
import tarfile

STAMP = 1707436800  # 2024-02-09: neither 1970 nor ahead of a TV's clock


def tar_gz(add):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        add(tar)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=STAMP) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def entry(tar, name, data=None, mode=0o644):
    info = tarfile.TarInfo(name)
    info.mtime, info.uid, info.gid, info.uname, info.gname = STAMP, 0, 0, "root", "root"
    if data is None:
        info.type, info.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(info)
    else:
        info.size, info.mode = len(data), mode
        tar.addfile(info, io.BytesIO(data))


def ar_member(name, data):
    head = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, STAMP, 0, 0, "100644", len(data))
    return head.encode() + data + (b"\n" if len(data) % 2 else b"")


def main(app_dir, out_dir):
    info = json.load(open(os.path.join(app_dir, "appinfo.json")))
    app_id, version = info["id"], info["version"]
    app_root = "usr/palm/applications/" + app_id
    pkg_root = "usr/palm/packages/" + app_id
    packageinfo = json.dumps({"id": app_id, "version": version, "app": app_id}, indent=2).encode()

    def data(tar):
        for d in ("usr/", "usr/palm/", "usr/palm/applications/", app_root + "/",
                  "usr/palm/packages/", pkg_root + "/"):
            entry(tar, d)
        for base, dirs, files in sorted(os.walk(app_dir)):
            dirs.sort()
            rel = os.path.relpath(base, app_dir)
            if rel != ".":
                entry(tar, app_root + "/" + rel + "/")
            for name in sorted(files):
                path = os.path.join(base, name)
                mode = 0o755 if os.access(path, os.X_OK) else 0o644
                entry(tar, app_root + "/" + os.path.normpath(os.path.join(rel, name)),
                      open(path, "rb").read(), mode)
        entry(tar, pkg_root + "/packageinfo.json", packageinfo)

    size = sum(os.path.getsize(os.path.join(b, f)) for b, _d, fs in os.walk(app_dir) for f in fs)
    control = ("Package: %s\nVersion: %s\nSection: misc\nPriority: optional\nArchitecture: all\n"
               "Installed-Size: %d\nMaintainer: N/A <nobody@example.com>\nDescription: %s\nwebOS-Package-Format-Version: 2\n"
               "webOS-Packager-Version: x.y.x\n") % (app_id, version, size, info["title"])

    def control_tar(tar):
        entry(tar, "control", control.encode())

    ipk = (b"!<arch>\n" + ar_member("debian-binary", b"2.0\n")
           + ar_member("control.tar.gz", tar_gz(control_tar)) + ar_member("data.tar.gz", tar_gz(data)))
    out = os.path.join(out_dir, "%s_%s_all.ipk" % (app_id, version))
    with open(out, "wb") as f:
        f.write(ipk)
    print(out)


if __name__ == "__main__":
    main(*sys.argv[1:3])
