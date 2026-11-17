"""Keep photo files only for the photo each case flags; every other photo keeps just its sha256.

The collector must download a photo to hash it, and photo_reuse needs only the hash. So after a
check of every scheme, the files under <data>/photos/ that are not the evidence photo of a case
(public or preview) are deleted. The hash stays in the records and the photo index, and the
collector does not download a photo again once its hash is known (collector/store.py known_photo).

If a later record reuses a deleted photo, that record's copy is downloaded under its own URL, so
the new case still has a file. Files younger than grace_hours are left alone, because their
records may not be saved yet when the checker reads them.

Cost: the vision check and any later check that needs the image itself see only kept photos."""
import os
import time
from pathlib import Path

GRACE_HOURS = 12


def keep_hashes(found_data, cases):
    """sha256 of each case's evidence photo: the reused photo of a photo_reuse case, the photo shown
    for a case from the other checks. found_data is accepted so callers need not change if a check
    later needs more photos kept."""
    return {c["_photo_src"]["sha256"] for c in cases
            if isinstance(c.get("_photo_src"), dict) and c["_photo_src"].get("sha256")}


def prune(photos_dir, keep, grace_hours=GRACE_HOURS, now=None):
    """Delete <photos_dir>/<xx>/<sha256>.<ext> files whose sha256 is not in keep.
    Returns (files deleted, bytes freed, files kept)."""
    photos_dir = Path(photos_dir)
    if not photos_dir.is_dir():
        return 0, 0, 0
    cutoff = (now if now is not None else time.time()) - grace_hours * 3600
    deleted = freed = kept = 0
    for sub in photos_dir.iterdir():
        if not sub.is_dir():
            continue
        for f in sub.iterdir():
            sha = f.name.split(".", 1)[0]
            if not f.is_file() or len(sha) != 64 or ".tmp" in f.name:
                continue
            if sha in keep:
                kept += 1
                continue
            try:
                st = f.stat()
                if st.st_mtime > cutoff:
                    kept += 1
                    continue
                os.remove(f)
            except OSError:
                kept += 1
                continue
            deleted += 1
            freed += st.st_size
    return deleted, freed, kept
