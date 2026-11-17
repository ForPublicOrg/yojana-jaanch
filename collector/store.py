"""On-disk state: records JSONL, coverage log, url->sha256 index, content-addressed photos.

Saving is cheap at scale: changed records are appended to the JSONL (a later line for the same
record_id wins, as in the checker), and coverage and photo index changes go to journals in
data/state/. A file is rewritten in full only when it has grown to twice its live size, when it is
small, or (coverage) at the end of a run. A full rewrite goes to a temporary file that is then
swapped in, so a crash or Ctrl+C leaves either the old or the new version. A line cut off by a
crash is set aside on the next start. Damaged files are rebuilt where possible.

Every file here belongs to one scheme, and DataLock(data_dir, scheme) lets one process at a time
write a scheme's files, so different schemes can run side by side in one data folder. The one
thing they share is the photo store, data/photos/<sha256[:2]>/<sha256>.<ext>: a photo is written
to a temporary file of its own and renamed into place, so two processes saving the same photo at
the same moment both end with the same whole file."""
import errno
import hashlib
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone

log = logging.getLogger("collector.store")

# Below this size the records file and photo index are rewritten in full at the end of a run.
SMALL_FILE = 32 * 1024 * 1024
MIN_JOURNAL = 1000


class _Tracked(dict):
    """A dict that remembers which keys were set since the last save."""

    def __init__(self, *a):
        super().__init__(*a)
        self.dirty = set()
        self.removed = False

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        self.dirty.add(k)

    def update(self, *a, **kw):
        for k, v in dict(*a, **kw).items():
            self[k] = v

    def __delitem__(self, k):
        super().__delitem__(k)
        self.dirty.discard(k)
        self.removed = True

    def pop(self, k, *default):
        if k in self:
            self.removed = True
            self.dirty.discard(k)
        return super().pop(k, *default)


class StoreError(Exception):
    """Local disk trouble (disk full, no permission). The run stops; files on disk stay consistent."""


def coverage_key(state, area_type, village_code):
    """Village and ward codes share one number range, so a code alone does not name one place."""
    cf = lambda x: " ".join(str(x or "").split()).casefold()
    return f"{cf(state)}|{cf(area_type)}|{village_code}"


def path_key(scheme, code_system, levels):
    """Coverage key of an area named by a level path: scheme, code system and every code from the top.
    Codes of different code systems (NREGA, LGD) are never compared, so the system is part of the key."""
    return f"{scheme}|{code_system or ''}|" + "/".join(str(lv["code"]) for lv in levels)


def legacy_name(scheme, base, ext):
    """jsjb keeps the file names it had before several schemes existed (last_run.json, queue.json);
    every other scheme gets base.<scheme>.ext."""
    return f"{base}.{ext}" if scheme == "jsjb" else f"{base}.{scheme}.{ext}"


def _entry_key(c):
    return c.get("key") or coverage_key(c.get("state"), c.get("area_type"), c["village_code"])


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _explain(e, path):
    if e.errno == errno.ENOSPC or getattr(e, "winerror", None) == 112:
        return f"disk is full while writing {path}; free some space and run again"
    if isinstance(e, PermissionError):
        return f"no permission to write {path} (or another program has it open); fix that and run again"
    return f"could not write {path}: {e}"


def _write_atomic(path, data):
    tmp = path + ".tmp"
    data = data.encode("utf-8") if isinstance(data, str) else data
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # On Windows a reader (the checker, an antivirus) can hold the target open, which blocks
        # the swap but not writing into the file itself.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    _write_in_place(tmp, path, data)
                    return
                time.sleep(0.5)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise StoreError(_explain(e, path)) from e


def store_shared(path, content):
    """Write a content-addressed file that other processes may be writing at the same moment.
    Each writer uses its own temporary name; the rename is atomic, and every writer has the same
    bytes, so whichever rename lands last leaves the same whole file."""
    try:
        if os.path.getsize(path) == len(content):
            return
    except OSError:
        pass
    tmp = f"{path}.{os.getpid()}-{secrets.token_hex(4)}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows refuses the rename while another process has the file open, for example
                # while it renames its own copy in. A whole file of the right size is the same photo.
                try:
                    if os.path.getsize(path) == len(content):
                        os.remove(tmp)
                        return
                except OSError:
                    pass
                if attempt == 4:
                    raise
                time.sleep(0.2)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise StoreError(_explain(e, path)) from e


def _write_in_place(tmp, path, data):
    """Overwrite path inside the open file. The full new copy waits in path.inplace until this is
    done, so an interrupted write is finished by _finish_in_place on the next start."""
    if not os.path.exists(path):
        raise PermissionError(errno.EACCES, "could not move the new file into place", path)
    keep = path + ".inplace"
    with open(path, "r+b") as f:
        os.replace(tmp, keep)
        f.write(data)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
    log.warning("%s is open in another program; it was overwritten in place", path)
    os.remove(keep)


def _append(path, data):
    data = data.encode("utf-8") if isinstance(data, str) else data
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        raise StoreError(_explain(e, path)) from e


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        # Replaying a journal over the file it was folded into changes nothing, so this is harmless.
        log.warning("could not remove %s (%s); it will be replayed on the next start", path, e)


def _read_journal(path, problems):
    """Whole JSON lines of a journal. A line cut off by a crash is dropped and that change is redone."""
    out, bad = [], 0
    if not os.path.exists(path):
        return out
    try:
        with open(path, "rb") as f:
            for raw in f:
                try:
                    out.append(json.loads(raw.decode("utf-8")))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    bad += 1
    except PermissionError as e:
        raise StoreError(f"no permission to read {path}: {e}") from e
    if bad:
        problems.append(f"{path}: skipped {bad} unreadable journal line(s); those changes will be redone")
    return out


def _finish_in_place(path, problems):
    keep = path + ".inplace"
    if not os.path.exists(keep):
        return
    try:
        with open(keep, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.remove(keep)
        problems.append(f"{path}: an earlier write was interrupted; finished it from {keep}")
    except OSError as e:
        raise StoreError(f"could not finish an interrupted write of {path} from {keep}: {e}") from e


def _set_aside(path, problems, why):
    """Keep a damaged file next to the original instead of overwriting it."""
    dest = f"{path}.damaged-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, dest)
        problems.append(f"{path}: {why}; moved to {dest} and rebuilt")
    except OSError as e:
        problems.append(f"{path}: {why}; could not move it aside ({e}), rebuilt in memory")


def _read_json(path, kind, problems):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except PermissionError as e:
        raise StoreError(f"no permission to read {path}: {e}") from e
    except (ValueError, UnicodeDecodeError, RecursionError) as e:
        _set_aside(path, problems, f"damaged ({e!r:.200})")
        return None
    if not isinstance(data, kind):
        _set_aside(path, problems, f"expected a JSON {kind.__name__}")
        return None
    return data


def image_type(content):
    """File extension for real image bytes, or None for anything else (HTML, empty, junk)."""
    c = content or b""
    if c.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if c.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if c[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if c[:4] == b"RIFF" and c[8:12] == b"WEBP":
        return ".webp"
    if c[:2] == b"BM" and len(c) > 26 and c[6:10] == b"\0\0\0\0":
        return ".bmp"
    if c[4:8] == b"ftyp" and c[8:12] in (b"heic", b"heix", b"mif1", b"heif"):
        return ".heic"
    return None


def image_complete(content):
    """False when bytes of a known image type end before the format says the file ends."""
    c = content or b""
    ext = image_type(c)
    if ext == ".jpg":
        # The last scan of a JPEG is closed by FFD9; neither marker can occur inside scan data.
        return c.find(b"\xff\xd9", max(c.rfind(b"\xff\xda"), 2)) != -1
    if ext == ".png":
        return b"IEND" in c[-32:]
    if ext == ".gif":
        return c.rstrip(b"\0").endswith(b";")
    if ext == ".webp":
        return int.from_bytes(c[4:8], "little") + 8 <= len(c)
    if ext == ".bmp":
        size = int.from_bytes(c[2:6], "little")
        return size == 0 or size <= len(c)
    return True


class Store:
    def __init__(self, data_dir, scheme):
        self.data_dir = data_dir
        self.scheme = scheme
        self.records_path = os.path.join(data_dir, "records", f"{scheme}.jsonl")
        self.coverage_path = os.path.join(data_dir, "records", f"{scheme}.coverage.json")
        self.index_path = os.path.join(data_dir, "state", f"{scheme}.photo_index.json")
        self.summary_path = os.path.join(data_dir, "state", legacy_name(scheme, "last_run", "json"))
        self.salt_path = os.path.join(data_dir, "state", f"{scheme}.salt")
        self.coverage_journal = os.path.join(data_dir, "state", f"{scheme}.coverage.journal.jsonl")
        self.index_journal = os.path.join(data_dir, "state", f"{scheme}.photo_index.journal.jsonl")
        self.problems = []
        self._rec_lines = 0
        self._rec_full = not os.path.exists(self.records_path)
        self._rec_newline = False
        for path in (self.records_path, self.coverage_path, self.index_path):
            _finish_in_place(path, self.problems)
        self.records = self._load_records()

        cov = _read_json(self.coverage_path, list, self.problems)
        self.coverage = {}
        dropped = 0
        for c in cov or []:
            if _entry_ok(c):
                self.coverage[_entry_key(c)] = c
            else:
                dropped += 1
        if dropped:
            self.problems.append(f"{self.coverage_path}: dropped {dropped} unreadable "
                                 "entries; those villages will be collected again")
        self._cov_full = cov is None
        if cov is None and self.records:
            self.coverage = self._coverage_from_records()
        journal = _read_journal(self.coverage_journal, self.problems)
        for c in journal:
            if _entry_ok(c):
                self.coverage[_entry_key(c)] = c
        self._cov_lines = len(journal)
        self.coverage = _Tracked(self.coverage)

        idx = _read_json(self.index_path, dict, self.problems)
        self._idx_full = idx is None
        if idx is None:
            idx = self._index_from_records() if self.records else {}
        journal = _read_journal(self.index_journal, self.problems)
        for e in journal:
            if isinstance(e, list) and len(e) == 2 and isinstance(e[0], str):
                idx[e[0]] = e[1]
        self._idx_lines = len(journal)
        self.photo_index = _Tracked({u: v for u, v in idx.items()
                                     if isinstance(v, dict) and "sha256" in v and (v["sha256"] is None or v.get("file"))})
        self.sha_files = {v["sha256"]: v["file"] for v in self.photo_index.values() if v["sha256"]}
        for p in self.problems:
            log.warning("%s", p)

    def _load_records(self):
        records, bad = {}, []
        if not os.path.exists(self.records_path):
            return _Tracked(records)
        try:
            f = open(self.records_path, "rb")
        except PermissionError as e:
            raise StoreError(f"no permission to read {self.records_path}: {e}") from e
        with f:
            raw = b"\n"
            for n, raw in enumerate(f, 1):
                self._rec_lines += 1
                if not raw.strip():
                    continue
                try:
                    r = json.loads(raw.decode("utf-8"))
                    if not isinstance(r, dict) or r.get("record_id") in (None, ""):
                        raise ValueError("not a record")
                except (ValueError, UnicodeDecodeError, RecursionError):
                    bad.append((n, raw))
                    continue
                records[str(r["record_id"])] = r
            self._rec_newline = not raw.endswith(b"\n")
        if bad:
            self._rec_full = True
            # The next flush rewrites the file, so keep the damaged lines somewhere a person can read them.
            keep = self.records_path + ".damaged-lines"
            try:
                with open(keep, "ab") as out:
                    for _, raw in bad:
                        out.write(raw.rstrip(b"\r\n") + b"\n")
            except OSError:
                keep = "nowhere (could not write the copy)"
            self.problems.append(f"{self.records_path}: skipped {len(bad)} damaged line(s) "
                                 f"(line {', '.join(str(n) for n, _ in bad[:10])}{', ...' if len(bad) > 10 else ''}); "
                                 f"copied to {keep}")
        return _Tracked(records)

    def _coverage_from_records(self):
        cov = {}
        for r in self.records.values():
            code = r.get("village_code")
            if not code or not _parse_time(r.get("last_seen")):
                continue
            c = cov.setdefault(r.get("area_key") or coverage_key(r.get("state"), r.get("area_type"), code), {
                "state": r.get("state"), "district": r.get("district"), "area_type": r.get("area_type"),
                "block": r.get("block"), "village": r.get("village"), "village_code": str(code),
                "records": 0, "collected_at": r["last_seen"]})
            c["records"] += 1
            c["collected_at"] = min(c["collected_at"], r["last_seen"])
        if cov:
            self.problems.append(f"coverage rebuilt from records: {len(cov)} villages; "
                                 "villages with no records will be collected again")
        return cov

    def _index_from_records(self):
        idx = {}
        for r in self.records.values():
            for p in r.get("photos") or []:
                if isinstance(p, dict) and p.get("url") and p.get("sha256") and p.get("file") \
                        and self.exists(p["file"]):
                    idx[p["url"]] = {"sha256": p["sha256"], "bytes": p.get("bytes"), "file": p["file"]}
        self.problems.append(f"photo index rebuilt from records: {len(idx)} photos; the rest will be downloaded again")
        return idx

    def area_key(self, area):
        if getattr(area, "levels", None):
            return path_key(self.scheme, area.code_system, area.levels)
        return coverage_key(area.state, area.area_type, area.village_code)

    def salt(self):
        """Secret for beneficiary_ref hashes, made once per data folder and scheme. It stays under
        data/ (not in git), so a published ref cannot be turned back into a registration number."""
        try:
            with open(self.salt_path, encoding="ascii") as f:
                value = f.read().strip()
            if len(value) >= 32:
                return value
        except FileNotFoundError:
            pass
        except (OSError, UnicodeDecodeError) as e:
            raise StoreError(f"could not read {self.salt_path}: {e}") from e
        value = secrets.token_hex(32)
        _write_atomic(self.salt_path, value + "\n")
        return value

    def photos_state(self, area):
        c = self.coverage.get(self.area_key(area))
        return c.get("photos_state") if c else None

    def last_collected(self, area):
        c = self.coverage.get(self.area_key(area))
        return _parse_time(c.get("collected_at")) if c else None

    def photos_failed(self, area):
        c = self.coverage.get(self.area_key(area))
        return bool(c and c.get("photos_failed"))

    def rel(self, path):
        try:
            path = os.path.relpath(path)
        except ValueError:
            path = os.path.abspath(path)
        return path.replace(os.sep, "/")

    def exists(self, file):
        """Photo paths are stored relative to the folder the collector ran in, usually the project root."""
        root = os.path.dirname(os.path.abspath(self.data_dir))
        return os.path.exists(file) or os.path.exists(os.path.join(root, file))

    def known_photo(self, url):
        """Index entry to reuse, or None when the photo should be downloaded (new, or its file is gone)."""
        e = self.photo_index.get(url)
        if e is None:
            return None
        if e["sha256"] and not self.exists(e["file"]):
            return None
        return e

    def save_photo(self, url, content, ext=None):
        """Store the bytes once per sha256 and, when the photo has a URL, remember it in the index.
        Photos sent inline (url None) are hashed and stored the same way but not indexed."""
        sha = hashlib.sha256(content).hexdigest()
        ext = ext or image_type(content) or ".jpg"
        existing = self.sha_files.get(sha)
        if existing and self.exists(existing):
            file = existing
        else:
            full = os.path.join(self.data_dir, "photos", sha[:2], sha + ext)
            store_shared(full, content)
            file = self.rel(full)
        self.sha_files[sha] = file
        entry = {"sha256": sha, "bytes": len(content), "file": file}
        if url:
            self.photo_index[url] = entry
        return entry

    def commit(self, area, records, seen_at, photos_failed=0, photos_state=None):
        """Save one whole village: its records and its coverage line together. Returns new record count.
        photos_state ("fetched" or "not_fetched") is kept in the coverage line when given."""
        prepared, new = [], 0
        key = self.area_key(area) if getattr(area, "levels", None) else None
        for record in records:
            rid = str(record["record_id"])
            old = self.records.get(rid)
            record["first_seen"] = old.get("first_seen", seen_at) if old else seen_at
            record["last_seen"] = seen_at
            if key:
                # A later --photos pass finds the coverage line of the record by this key.
                record["area_key"] = key
            prepared.append((rid, record))
            new += old is None
        self.records.update(prepared)
        self.mark_covered(area, records, seen_at, photos_failed, photos_state)
        return new

    def mark_covered(self, area, records, at, photos_failed=0, photos_state=None):
        # Prefer names as spelled in the records; dropdowns may differ in case (e.g. "PUNJAB").
        # The key is kept in the entry because record names may be spelled unlike the dropdowns.
        r = records[0] if records else {}
        key = self.area_key(area)
        entry = {
            "state": r.get("state") or area.state, "district": r.get("district") or area.district,
            "area_type": r.get("area_type") or area.area_type, "block": r.get("block") or area.block,
            "village": r.get("village") or area.village, "village_code": str(area.village_code),
            "records": len(records), "collected_at": at, "key": key}
        if getattr(area, "levels", None):
            entry["levels"] = area.levels
            entry["code_system"] = area.code_system
        if photos_failed:
            entry["photos_failed"] = photos_failed
        if photos_state:
            entry["photos_state"] = photos_state
        self.coverage[key] = entry

    def write_record(self, record):
        """Replace one saved record (the --photos pass). first_seen and last_seen are kept."""
        self.records[str(record["record_id"])] = record

    def flush(self, final=True):
        """Save what changed. final=False is the cheap save during a run; final=True (end of a run)
        also folds the coverage journal into the coverage file the checker reads.
        Records first and coverage last: a crash in between never claims a village whose records are missing."""
        self._flush_records(final)
        self._flush_index(final)
        self._flush_coverage(final)

    def _flush_records(self, final):
        recs = self.records
        try:
            size = os.path.getsize(self.records_path)
        except OSError:
            size = 0
        dupes = self._rec_lines - len(recs)
        if self._rec_full or recs.removed or dupes > max(len(recs), MIN_JOURNAL) \
                or (final and size < SMALL_FILE and (recs.dirty or dupes > 0)):
            lines = (json.dumps(r, ensure_ascii=False) for r in
                     sorted(recs.values(),
                            key=lambda r: (str(r.get("village_code") or ""), str(r["record_id"]).zfill(20))))
            _write_atomic(self.records_path, "".join(l + "\n" for l in lines))
            self._rec_lines, self._rec_full, self._rec_newline = len(recs), False, False
        elif recs.dirty:
            data = "".join(json.dumps(recs[k], ensure_ascii=False) + "\n" for k in sorted(recs.dirty) if k in recs)
            try:
                _append(self.records_path, ("\n" if self._rec_newline else "") + data)
            except StoreError:
                # Part of a line may have reached the disk; start the next append on a fresh line.
                self._rec_newline = True
                raise
            self._rec_lines += len(recs.dirty)
            self._rec_newline = False
        recs.dirty.clear()
        recs.removed = False

    def _flush_index(self, final):
        idx = self.photo_index
        try:
            size = os.path.getsize(self.index_path)
        except OSError:
            size = 0
        if self._idx_full or idx.removed or self._idx_lines + len(idx.dirty) > max(len(idx), MIN_JOURNAL) \
                or (final and size < SMALL_FILE and (idx.dirty or self._idx_lines)):
            _write_atomic(self.index_path, json.dumps(idx, indent=0))
            _remove(self.index_journal)
            self._idx_full, self._idx_lines = False, 0
        elif idx.dirty:
            _append(self.index_journal, "".join(json.dumps([u, idx[u]]) + "\n" for u in sorted(idx.dirty) if u in idx))
            self._idx_lines += len(idx.dirty)
        idx.dirty.clear()
        idx.removed = False

    def _flush_coverage(self, final):
        cov = self.coverage
        if self._cov_full or cov.removed or self._cov_lines + len(cov.dirty) > max(len(cov), MIN_JOURNAL) \
                or (final and (cov.dirty or self._cov_lines)):
            _write_atomic(self.coverage_path, json.dumps(list(cov.values()), ensure_ascii=False, indent=1))
            _remove(self.coverage_journal)
            self._cov_full, self._cov_lines = False, 0
        elif cov.dirty:
            _append(self.coverage_journal,
                    "".join(json.dumps(cov[k], ensure_ascii=False) + "\n" for k in sorted(cov.dirty) if k in cov))
            self._cov_lines += len(cov.dirty)
        cov.dirty.clear()
        cov.removed = False

    def write_summary(self, summary):
        _write_atomic(self.summary_path, json.dumps(summary, ensure_ascii=False, indent=1))


def _entry_ok(c):
    return isinstance(c, dict) and bool(c.get("village_code") or c.get("key")) \
        and _parse_time(c.get("collected_at")) is not None


def _parse_time(s):
    if not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class DataInUse(StoreError):
    """Another collector or queue is writing to the same data folder."""


LEGACY_LOCK = {"jsjb": "collector.lock"}


class DataLock:
    """Only one collector or queue per scheme may write to a data folder at a time. The operating
    system drops the lock when the process ends, even after a crash, so a stale lock file never
    blocks a run.

    DataLock(data_dir, scheme) holds data/state/collector.<scheme>.lock, so other schemes can run at
    the same time. For jsjb it also holds data/state/collector.lock, the lock that JSJB runs took
    before there were several schemes, so a JSJB run started with the older code still refuses a
    new one and the other way round. DataLock(data_dir) holds only data/state/collector.lock."""

    def __init__(self, data_dir, scheme=None):
        if scheme is None:
            names = ["collector.lock"]
        else:
            names = ([LEGACY_LOCK[scheme]] if scheme in LEGACY_LOCK else []) + [f"collector.{scheme}.lock"]
        self.files = []
        try:
            for name in names:
                self.path = os.path.join(data_dir, "state", name)
                self.files.append(self._take(data_dir, self.path))
        except BaseException:
            self.release()
            raise

    @staticmethod
    def _take(data_dir, path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            f = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)), "r+b")
        except OSError as e:
            raise StoreError(_explain(e, path)) from e
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise DataInUse(f"another collector or queue is already writing to {data_dir} "
                            f"(lock held on {path}); stop it first, or wait for it to finish") from None
        try:
            f.seek(0, os.SEEK_END)
            if os.name != "nt":
                f.truncate(0)
            f.write(f"{os.getpid()} {now_iso()}\n".encode())
            f.flush()
        except OSError:
            pass
        return f

    def release(self):
        for f in reversed(self.files):
            if f.closed:
                continue
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            f.close()
