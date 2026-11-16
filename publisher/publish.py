"""python -m publisher publish: blurred public copies of the case photos in publish/photos/."""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import common, review


def find_original(paths, sha, rel):
    candidates = [paths.root / "site" / rel]
    sub = paths.data_photos / sha[:2]
    if sub.is_dir():
        candidates += sorted(sub.glob(sha + ".*"))
    candidates += sorted(paths.data_photos.glob(sha + ".*")) if paths.data_photos.is_dir() else []
    for c in candidates:
        if c.is_file() and c.suffix.lower() not in (".part", ".tmp"):
            return c
    return None


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"}


def stray_images(paths):
    """Image files under publish/ that are not blurred copies: they could be unblurred originals."""
    if not paths.publish.is_dir():
        return []
    keep = (paths.out_photos.resolve(), paths.upload.resolve())
    return sorted(f for f in paths.publish.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_EXT
                  and not any(f.resolve().is_relative_to(k) for k in keep))


def run(paths, allow_download=True, detector=None, log=print):
    from . import faces

    if not paths.cases.exists():
        log(f"{paths.cases} not found. Run a refresh first: python run.py refresh ...")
        return 1
    common.ensure_hand_files(paths)
    wanted = common.case_photo_hashes(paths.cases)
    hidden = common.read_hide(paths.hide)
    extra = common.read_extra_blur(paths.extra_blur)
    settings = common.read_settings(paths.settings)
    manifest = common.read_manifest(paths.manifest)
    if detector is None:
        try:
            detector = faces.Detector(allow_download=allow_download, log=log)
        except Exception as e:
            log(f"Stopped: the detection models could not be loaded ({e}). "
                "See publisher/models/SOURCE.md; nothing was changed.")
            return 2
    log(f"Detector {detector.name}, settings version {detector.version}")

    removed = 0
    for sha in list(manifest):
        if sha in hidden or sha not in wanted:
            del manifest[sha]
            removed += 1
    for f in paths.out_photos.glob("*") if paths.out_photos.is_dir() else []:
        if f.stem not in manifest or f.suffix != ".jpg":
            f.unlink()

    todo = []
    for sha, rel in sorted(wanted.items()):
        if sha in hidden:
            continue
        e = manifest.get(sha)
        if (e and e.get("settings_version") == detector.version and e.get("extra_boxes", []) == extra.get(sha, [])
                and (paths.out_photos / f"{sha}.jpg").exists()):
            continue
        todo.append((sha, rel))
    log(f"{len(wanted)} photos in cases.json, {len(hidden & set(wanted))} hidden, "
        f"{len(todo)} to process, {removed} removed from the manifest")

    missing = bad = 0
    t0 = time.time()
    for i, (sha, rel) in enumerate(todo, 1):
        src = find_original(paths, sha, rel)
        if not src:
            missing += 1
            log(f"  original not found for {sha[:12]}")
            continue
        if faces.sha256_file(src) != sha:
            bad += 1
            log(f"  {src.name}: bytes do not match the sha256 in its name; skipped")
            continue
        try:
            regions, signal, w, h = faces.make_public_copy(src, paths.out_photos / f"{sha}.jpg", detector,
                                                           extra.get(sha, []))
        except ValueError as e:
            bad += 1
            log(f"  {sha[:12]}: {e}")
            continue
        manifest[sha] = {"sha256": sha, "copy_sha256": common.file_hashes(paths.out_photos / f"{sha}.jpg")[0],
                         "faces": len(regions), "boxes": [r["box"] for r in regions],
                         "box_engines": [r["engine"] for r in regions], "box_kinds": [r["kind"] for r in regions],
                         "people_signal": signal, "extra_boxes": extra.get(sha, []),
                         "width": w, "height": h, "detector": detector.name,
                         "settings_version": detector.version,
                         "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if i % 20 == 0:
            common.write_manifest(paths.manifest, manifest)
            log(f"  {i}/{len(todo)} done, {time.time() - t0:.0f}s")
    for sha, e in manifest.items():
        if not e.get("copy_sha256") and (paths.out_photos / f"{sha}.jpg").exists():
            e["copy_sha256"] = common.file_hashes(paths.out_photos / f"{sha}.jpg")[0]
    common.write_manifest(paths.manifest, manifest)
    approved = common.read_approved(paths.approved)
    auto = common.auto_approve(paths.approved, manifest, approved, hidden, settings["auto_approve_no_person"])
    ready = common.sync_upload(paths, manifest, approved, hidden)
    review.write(paths, manifest, approved, settings)

    with_faces = sum(1 for e in manifest.values() if e["faces"])
    log(f"Blurred copies: {len(manifest)} ({with_faces} with at least one face or head blurred, "
        f"{len(manifest) - with_faces} with none). Missing originals: {missing}. Skipped: {bad}.")
    if settings["auto_approve_no_person"]:
        log(f"auto_approve_no_person is on: {auto} copies with no person found were approved without a check.")
    log(f"Approved and copied to {paths.upload}: {len(ready)}. Waiting for a check: {len(manifest) - len(ready)}.")
    log(f"The engines can still miss a face. Look at every photo in {paths.review} and upload only {paths.upload}.")
    stray = stray_images(paths)
    if stray:
        log(f"WARNING: {len(stray)} image files in {paths.publish} are not blurred copies and may show faces. "
            f"Move them out of this folder: " + ", ".join(str(f.relative_to(paths.publish)) for f in stray[:10])
            + (" ..." if len(stray) > 10 else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m publisher publish", description=__doc__)
    ap.add_argument("--root", default=str(common.ROOT))
    ap.add_argument("--publish-dir", help="default: <root>/publish")
    ap.add_argument("--no-download", action="store_true", help="do not fetch the model files if they are missing")
    a = ap.parse_args(argv)
    paths = common.Paths(Path(a.root), a.publish_dir)
    return run(paths, allow_download=not a.no_download)


if __name__ == "__main__":
    sys.exit(main())
