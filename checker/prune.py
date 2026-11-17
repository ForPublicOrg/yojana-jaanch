"""Keep photo files only for the photos each case flags; every other photo keeps just its sha256.

The collector must download a photo to hash it, and the exact-file part of photo_reuse needs only
the hash. So after a check of every scheme, the files under <data>/photos/ that are not the
evidence photo of a case (public or preview) are deleted. The hash stays in the records and the
photo index, and the collector does not download a photo again once its hash is known
(collector/store.py known_photo).

The near-duplicate part of photo_reuse needs each file decoded once by the phash step
(python run.py phash), which writes <data>/state/phash.jsonl. Once that file exists, a file whose
sha256 has no line in it is never deleted, so the phash step always sees a file before it goes.
Every file of a picture saved as several files is kept, not only the one shown, so a person can
compare them in data/preview/near_matches.html. Without phash.jsonl files are deleted as before,
and the checker says that near-duplicate matching needs the phash step.

If a later record reuses a deleted photo, that record's copy is downloaded under its own URL, so
the new case still has a file. Files younger than grace_hours are left alone, because their
records may not be saved yet when the checker reads them.

Cost: the vision check and any later check that needs the image itself see only kept photos."""
import os
import time
from pathlib import Path

# A village's records are saved within minutes of its photos (the collector holds at most 3 villages
# of downloaded photos before saving them). 12 hours kept a day's downloads on disk between checks:
# measured 18 Nov 2026, 101,045 files kept and 13 GB left free.
GRACE_HOURS = 3


def keep_hashes(found_data, cases):
    """sha256 of each case's evidence photo (the reused photo of a photo_reuse case, the photo shown
    for a case from the other checks) and of every file of a picture saved as several files.
    found_data is accepted so callers need not change if a check later needs more photos kept."""
    out = {c["_photo_src"]["sha256"] for c in cases
           if isinstance(c.get("_photo_src"), dict) and c["_photo_src"].get("sha256")}
    for c in cases:
        out.update(c.get("_member_shas") or [])
    return out


def prune(photos_dir, keep, grace_hours=GRACE_HOURS, now=None, hashed=None):
    """Delete <photos_dir>/<xx>/<sha256>.<ext> files whose sha256 is not in keep. With hashed (the
    sha256 values in phash.jsonl), a file whose sha256 is not in it is kept as well.
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
            if sha in keep or (hashed is not None and sha not in hashed):
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
