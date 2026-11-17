"""python -m publisher drive-copy: put the approved blurred copies into the Drive sync folder, by subfolder.

Each copy goes to <local folder>/<first character of its name>/<sha>.jpg, so that no folder grows past
what Drive's public page can list (drive.PAGE_LIMIT files). A copy already at the top of the folder is
moved into its subfolder; Drive keeps the file, its id and its sharing when it is moved.

Only copies that drive-links would link are copied: approved in approved.txt, not in hide.txt, and
with the bytes recorded in publish/photos. Nothing is deleted and no file with other bytes is replaced;
those are listed instead.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

from . import common
from .drive import IGNORED, NAME_RE, approved_copies, subfolder_for


def run(paths, local_folder, log=print):
    local_folder = Path(local_folder)
    if not local_folder.is_dir():
        log(f"{local_folder} is not a folder.")
        return 2
    manifest, hidden, expected, allowed = approved_copies(paths)
    if not manifest:
        log(f"{paths.manifest} is empty or missing. Run python run.py publish first.")
        return 1
    copied = moved = present = 0
    problems = []
    for sha in sorted(allowed):
        want = expected[sha]["sha256"]
        if want == sha:
            problems.append((f"{sha}.jpg", "the published copy has the bytes of the original, so it was not copied"))
            continue
        sub = local_folder / subfolder_for(sha)
        dest = sub / f"{sha}.jpg"
        top = local_folder / f"{sha}.jpg"
        if dest.exists():
            if common.file_hashes(dest)[0] == want:
                present += 1
            else:
                problems.append((f"{sub.name}/{dest.name}", "has other bytes than the approved copy; not replaced"))
            if top.exists():
                problems.append((top.name, f"is also in {sub.name}/; one of the two should be deleted from Drive"))
            continue
        sub.mkdir(exist_ok=True)
        if top.is_file() and common.file_hashes(top)[0] == want:
            os.replace(top, dest)
            moved += 1
            continue
        if top.exists():
            problems.append((top.name, "has other bytes than the approved copy; left where it is"))
        tmp = sub / f".{sha}.jpg.part"
        shutil.copyfile(paths.out_photos / f"{sha}.jpg", tmp)
        if common.file_hashes(tmp)[0] != want:
            tmp.unlink()
            problems.append((dest.name, "the copy did not match publish/photos after copying; not copied"))
            continue
        os.replace(tmp, dest)
        copied += 1
    others = sorted(p.name for p in local_folder.iterdir()
                    if p.is_file() and p.name.lower() not in IGNORED and NAME_RE.match(p.name.lower())
                    and NAME_RE.match(p.name.lower()).group(1) not in allowed)
    log(f"{len(allowed)} approved copies: {copied} copied, {moved} moved into their subfolder, "
        f"{present} already there.")
    if others:
        log(f"{len(others)} photos at the top of {local_folder} are not approved copies and were left alone; "
            f"drive-links lists the reason for each.")
    for name, why in problems:
        log(f"  {name}: {why}")
    return 4 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m publisher drive-copy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local-folder", required=True, help="the Google Drive sync folder on this computer")
    ap.add_argument("--root", default=str(common.ROOT))
    ap.add_argument("--publish-dir")
    a = ap.parse_args(argv)
    return run(common.Paths(Path(a.root), a.publish_dir), a.local_folder)


if __name__ == "__main__":
    sys.exit(main())
