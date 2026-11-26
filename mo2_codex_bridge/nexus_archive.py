"""Verify an MO2 2.5.2 Nexus download without installing or modifying it."""
import ctypes
from contextlib import contextmanager
from ctypes import wintypes
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import stat


_ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".omod", ".fomod"}
_UNFINISHED = ".unfinished"
_META = ".meta"
_MAX_DIRECTORY_ENTRIES = 8192
_MAX_META_BYTES = 1024 * 1024
_MAX_SCAN_META_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 128 * 1024 ** 3
_ARCHIVE_CHUNK = 1024 * 1024
_REPARSE_POINT = 0x400
_FIELDS = {"gamename", "modid", "fileid", "repository", "paused", "removed"}
_WINDOWS_DEVICE = re.compile(r"(?i)^(?:CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])$")


def _plain_filename(name):
    return (name == name.strip() and 0 < len(name) <= 255 and not name.endswith(".")
            and not _WINDOWS_DEVICE.fullmatch(name.split(".", 1)[0].rstrip(" "))
            and not any(c in '<>:"/\\|?*' or ord(c) < 32 or ord(c) == 127 for c in name))


def _lexical(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _root(downloads_root):
    path = Path(downloads_root)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("The MO2 download directory must be an absolute local path")
    if os.name == "nt" and not re.fullmatch(r"[A-Za-z]:\\", path.anchor):
        raise ValueError("The MO2 download directory must be on a local drive")
    if os.name == "nt" and _windows_files().GetDriveTypeW(path.anchor) != 3:  # DRIVE_FIXED
        raise ValueError("The MO2 download directory must be on a fixed local drive")
    for parent in reversed((path, *path.parents)):
        info = parent.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("The MO2 download directory has a linked or unsafe path component")
    if not info.st_ino:
        raise ValueError("The MO2 download directory does not expose a stable identity")
    return path, [str(info.st_dev), str(info.st_ino)]


def _file_info(path):
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino:
        raise ValueError("Linked or nonregular download files are not read")
    return info


def _fingerprint(info):
    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
    # Windows handle/path ctime semantics can differ on supported Pythons.
    return signature if os.name == "nt" else (*signature, info.st_ctime_ns)


@lru_cache(maxsize=1)
def _windows_files():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    wintypes.LPVOID, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                 wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


class _FileAttributeTag(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [("creation", ctypes.c_int64), ("access", ctypes.c_int64),
                ("write", ctypes.c_int64), ("change", ctypes.c_int64),
                ("attributes", wintypes.DWORD)]


class _VerificationCancelled(RuntimeError):
    pass


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise _VerificationCancelled


def _read_handle(path):
    if os.name != "nt":
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    import msvcrt

    kernel = _windows_files()
    # Existing file, no reparse traversal on the leaf, and compatible with
    # readers only. Reject an open writer rather than hashing live bytes.
    name = "\\\\?\\" + str(path)
    handle = kernel.CreateFileW(name, 0x80000000, 1, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        tag = _FileAttributeTag()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise ctypes.WinError(ctypes.get_last_error())
        if tag.attributes & _REPARSE_POINT:
            raise ValueError("A reparse point cannot be verified as an archive")
        final = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        if not 0 < length < len(final) or _lexical(final.value) != _lexical(name):
            raise ValueError("The download handle did not resolve to its expected local path")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return fd


@contextmanager
def _stable_read(path, limit):
    before = _file_info(path)
    if not 0 < before.st_size <= limit:
        raise ValueError("Download file is empty or exceeds its verification limit")
    signature = _fingerprint(before)
    fd = _read_handle(path)
    try:
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        if _fingerprint(os.fstat(fd)) != signature:
            raise ValueError("Download file changed before it could be read")
        yield stream, before.st_size
        if (_fingerprint(os.fstat(fd)) != signature
                or _fingerprint(_file_info(path)) != signature):
            raise ValueError("Download file changed while being read")


def _handle_signature(stream):
    fd = stream.fileno()
    signature = _fingerprint(os.fstat(fd))
    if os.name == "nt":
        import msvcrt

        info = _FileBasicInfo()
        if not _windows_files().GetFileInformationByHandleEx(
                msvcrt.get_osfhandle(fd), 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not info.change:
            raise ValueError("The download file does not expose a stable change timestamp")
        signature = (*signature, info.change)
    return [str(value) for value in signature]


def _directory_signature(root, identity):
    if _root(root)[1] != identity:
        raise ValueError("The MO2 download directory changed during archive verification")
    info = root.lstat()
    if _linked(info) or not stat.S_ISDIR(info.st_mode) or [str(info.st_dev), str(info.st_ino)] != identity:
        raise ValueError("The MO2 download directory changed during archive verification")
    return [*identity, str(info.st_mtime_ns), str(info.st_ctime_ns)]


def _read_meta(path, budget):
    with _stable_read(path, _MAX_META_BYTES) as (stream, size):
        if budget[0] < size:
            raise RuntimeError("Download metadata scan exceeds its read budget")
        raw = stream.read(size + 1)
        budget[0] -= len(raw)
        if len(raw) != size:
            raise ValueError("Download metadata changed while being read")
    return _metadata(raw)


def _metadata(raw):
    # QSettings IniFormat stores the root keys in [General]. The identity
    # fields are canonical ASCII values. Ignore URLs and arbitrary QVariant
    # payloads rather than interpreting them as part of the receipt.
    inside = False
    seen_general = False
    values = {}
    for line in raw.decode("utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("["):
            inside = line == "[General]"
            if inside and seen_general:
                raise ValueError("Duplicate General section in download metadata")
            seen_general = seen_general or inside
        elif inside and not line.startswith(("#", ";")):
            key, sep, value = line.partition("=")
            field = key.strip().casefold()
            if sep and field in _FIELDS:
                if field in values:
                    raise ValueError("Duplicate identity field in download metadata")
                values[field] = value.strip()
    return values


def _selector(target):
    game, short = target["game"], target["game_short"]
    mod_id, file_id = target["mod_id"], target["file_id"]
    if (not isinstance(game, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", game)
            or not isinstance(short, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", short)
            or any(type(n) is not int or not 1 <= n <= 2147483647 for n in (mod_id, file_id))):
        raise ValueError("An exact MO2 Nexus game, mod ID and file ID are required")
    size = target.get("size_bytes")
    if size is not None and (type(size) is not int or not 0 < size <= _MAX_ARCHIVE_BYTES):
        raise ValueError("Invalid expected Nexus archive byte count")
    return {game, short.casefold()}, str(mod_id), str(file_id)


def _matches(values, selector):
    games, mod_id, file_id = selector
    return (values.get("repository") == "Nexus" and values.get("gamename", "").casefold() in games
            and values.get("modid") == mod_id and values.get("fileid") == file_id)


def _has_identity(values):
    repository = values.get("repository", "")
    if repository != "Nexus":
        return bool(repository) and repository.casefold() != "nexus"
    return (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", values.get("gamename", "")) is not None
            and all(re.fullmatch(r"[1-9][0-9]{0,9}", values.get(key, ""))
                    and int(values[key]) <= 2147483647 for key in ("modid", "fileid")))


def _download_name(name):
    folded = name.casefold()
    plain = folded.removesuffix(_META).removesuffix(_UNFINISHED)
    if Path(plain).suffix in _ARCHIVE_EXTENSIONS:
        return plain
    return None


def _scan(path, selector, *, cancelled=None, previous_names=None, expected=None):
    files = {}
    with os.scandir(path) as entries:
        for count, entry in enumerate(entries, start=1):
            _check_cancelled(cancelled)
            if count > _MAX_DIRECTORY_ENTRIES:
                raise ValueError("The MO2 download directory exceeds the verification entry limit")
            name = entry.name
            if _download_name(name) is None:
                continue
            folded = name.casefold()
            if folded in files:
                raise ValueError("The download directory contains ambiguous case-equivalent names")
            files[folded] = name
    rows = {}
    if selector is None:
        return files, rows
    required = set()
    if previous_names is not None:
        for name in files:
            alias = _download_name(name)
            if alias == expected or alias not in previous_names:
                required.add(name if name.endswith(_META) else name + _META)
    unclassified = required.copy()
    budget = [_MAX_SCAN_META_BYTES]
    for name_key, name in files.items():
        _check_cancelled(cancelled)
        if not name_key.endswith(_META):
            continue
        archive_key = _download_name(name)
        try:
            if not _plain_filename(name):
                raise ValueError("Unsafe MO2 download metadata name")
            values = _read_meta(path / name, budget)
            if name_key in required:
                if not _has_identity(values):
                    raise ValueError("The metadata does not identify its download")
                unclassified.discard(name_key)
        except (OSError, ValueError):
            if name_key in required:
                raise ValueError("Relevant MO2 download metadata could not be identified safely") from None
            continue
        if not _matches(values, selector):
            continue
        ready = (name_key == archive_key + _META and archive_key in files
                 and archive_key + _UNFINISHED not in files
                 and values.get("paused") == "false" and values.get("removed") == "false")
        if ready or archive_key not in rows:
            # MO2 can leave the old .unfinished.meta after renaming its
            # payload. A finished sidecar plus no unfinished payload wins.
            rows[archive_key] = (files.get(archive_key, name[:-len(_META)]), ready)
    if unclassified:
        raise ValueError("A newer or expected download has no readable identifying metadata")
    return files, rows


def capture_archive_baseline(downloads_root, target=None):
    """Capture existing aliases and exact-file rows before queueing or clicking."""
    root, identity = _root(downloads_root)
    files, rows = _scan(root, _selector(target) if target is not None else None)
    if _root(root)[1] != identity:
        raise ValueError("The MO2 download directory changed during baseline capture")
    # An existing archive or either sidecar reserves the base name. Neither
    # adding an exact metadata file to an old archive nor finishing a
    # pre-request partial should make it a new, attributable result.
    return {
        "root": str(root), "root_identity": identity,
        "names": sorted({_download_name(name) for name in files}),
        "existing": [{"name": name, "state": "complete" if ready else "incomplete"}
                     for name, ready in rows.values()],
    }


def _result(status, message):
    return {"status": status, "message": message}


def _matching_root(downloads_root, baseline):
    root, identity = _root(downloads_root)
    if (_lexical(root) != _lexical(baseline["root"]) or identity != baseline["root_identity"]
            or not isinstance(baseline["names"], list) or len(baseline["names"]) > _MAX_DIRECTORY_ENTRIES
            or any(not isinstance(name, str) for name in baseline["names"])):
        raise ValueError("The MO2 download directory no longer matches this request")
    return root, identity


def capture_retry_baseline(downloads_root, target, previous_baseline, *, expected_name=None):
    """Exclude only an exact leftover temporary sidecar from a fresh attempt's baseline."""
    root, identity = _matching_root(downloads_root, previous_baseline)
    selector = _selector(target)
    previous_names = set(previous_baseline["names"])
    expected = None
    if expected_name is not None:
        if (not isinstance(expected_name, str) or not _plain_filename(expected_name)
                or Path(expected_name).suffix.casefold() not in _ARCHIVE_EXTENSIONS):
            raise ValueError("The exact Nexus archive filename is unsafe or unsupported")
        expected = expected_name.casefold()
    before = _directory_signature(root, identity)
    files, rows = _scan(root, selector, previous_names=previous_names, expected=expected)
    names = {_download_name(name) for name in files}
    if len(rows) > 1:
        raise ValueError("Multiple downloads have the requested Nexus file identity")
    if rows:
        key = next(iter(rows))
        orphan_key = key + _UNFINISHED + _META
        if (key in previous_names or orphan_key not in files
                or any(candidate in files for candidate in (key, key + _UNFINISHED, key + _META))):
            raise ValueError("The exact Nexus download still has an archive, partial file, or unsafe metadata in MO2")
        values = _read_meta(root / files[orphan_key], [_MAX_META_BYTES])
        if (not _matches(values, selector) or values.get("removed") != "false"
                or values.get("paused") not in ("true", "false")):
            raise ValueError("The leftover MO2 download metadata could not be confirmed")
        names.remove(key)
    if expected in names:
        raise ValueError("The expected Nexus filename is already present in MO2 Downloads")
    if _directory_signature(root, identity) != before:
        raise ValueError("The download directory changed during retry verification")
    return {"root": str(root), "root_identity": identity, "names": sorted(names), "existing": [],
            "_directory_proof": before}


def inspect_nexus_archive(downloads_root, target, baseline, *, cancelled=None):
    """Read the exact new final archive and return a stable local SHA-256."""
    try:
        return _inspect_archive(downloads_root, target, baseline, cancelled)
    except _VerificationCancelled:
        return _result("cancelled", "Archive verification was interrupted while the bridge closed.")


def _inspect_archive(downloads_root, target, baseline, cancelled):
    _check_cancelled(cancelled)
    selector = _selector(target)
    root, identity = _matching_root(downloads_root, baseline)
    before = _directory_signature(root, identity)
    files, rows = _scan(root, selector, cancelled=cancelled)
    if not rows:
        return {**_result("waiting", "No download with the requested Nexus file identity is visible yet."),
                "_directory_proof": before}
    if len(rows) != 1:
        return _result("ambiguous", "Multiple downloads have the requested Nexus file identity.")
    archive_key, (name, ready) = next(iter(rows.items()))
    if archive_key in baseline["names"]:
        return _result("preexisting", "This download name was already present before the request.")
    if not ready:
        if archive_key not in files and archive_key + _UNFINISHED not in files:
            return {**_result("metadata_only", "MO2 has identifying metadata but no archive or partial file for this request."),
                    "_directory_proof": before}
        return _result("incomplete", "MO2 has no ready archive for this Nexus file yet.")
    if not _plain_filename(name):
        return _result("unverified", "The matching download has an unsafe filename.")
    path = root / name
    meta = path.with_name(path.name + _META)
    _check_cancelled(cancelled)
    try:
        # Pin the archive and its exact final metadata together while
        # checking both identities. Re-scan after hashing for newly competing
        # candidates and unfinished transfers before handing off the path.
        with _stable_read(path, _MAX_ARCHIVE_BYTES) as (stream, size):
            with _stable_read(meta, _MAX_META_BYTES) as (meta_stream, meta_size):
                _check_cancelled(cancelled)
                raw = meta_stream.read(meta_size + 1)
                values = _metadata(raw)
                if (len(raw) != meta_size or not _matches(values, selector)
                        or values.get("paused") != "false" or values.get("removed") != "false"):
                    raise ValueError("The matching download metadata changed")
                if target.get("size_bytes") not in (None, size):
                    return _result("unverified", "The archive byte count differs from the Nexus file list.")
                digest = hashlib.sha256()
                hashed = 0
                while True:
                    _check_cancelled(cancelled)
                    chunk = stream.read(_ARCHIVE_CHUNK)
                    if not chunk:
                        break
                    hashed += len(chunk)
                    if hashed > size:
                        raise ValueError("The matching archive grew while being hashed")
                    digest.update(chunk)
                mark = _directory_signature(root, identity)
                if (hashed != size or _scan(root, selector, cancelled=cancelled)[1] != rows
                        or _directory_signature(root, identity) != mark):
                    raise ValueError("Download evidence changed during archive verification")
                proof = {
                    "directory": mark, "archive": _handle_signature(stream),
                    "metadata": _handle_signature(meta_stream),
                }
    except (OSError, ValueError):
        return _result("unverified", "The matching archive or metadata could not be read and verified safely.")
    _check_cancelled(cancelled)
    return {
        "status": "verified",
        "message": "Verified the completed MO2 Nexus archive.",
        "archive": {"name": name, "path": str(path), "bytes": size, "sha256": digest.hexdigest()},
        "_proof": proof,
    }


def revalidate_verified_archive(downloads_root, target, baseline, evidence):
    """Repin worker-verified evidence on MO2's GUI thread before publication."""
    try:
        selector = _selector(target)
        root, identity = _matching_root(downloads_root, baseline)
        candidate, proof = evidence["archive"], evidence["_proof"]
        name = candidate["name"]
        if (evidence["status"] != "verified" or not _plain_filename(name)
                or Path(name).suffix.casefold() not in _ARCHIVE_EXTENSIONS
                or name.casefold() in baseline["names"]
                or not re.fullmatch(r"[a-f0-9]{64}", candidate["sha256"])
                or _directory_signature(root, identity) != proof["directory"]):
            return False
        path = root / name
        if _lexical(path) != _lexical(candidate["path"]):
            return False
        meta = path.with_name(name + _META)
        with _stable_read(path, _MAX_ARCHIVE_BYTES) as (stream, size):
            with _stable_read(meta, _MAX_META_BYTES) as (meta_stream, meta_size):
                raw = meta_stream.read(meta_size + 1)
                values = _metadata(raw)
                if (size != candidate["bytes"] or target.get("size_bytes") not in (None, size)
                        or len(raw) != meta_size or not _matches(values, selector)
                        or values.get("paused") != "false" or values.get("removed") != "false"
                        or _handle_signature(stream) != proof["archive"]
                        or _handle_signature(meta_stream) != proof["metadata"]):
                    return False
                try:
                    path.with_name(name + _UNFINISHED).lstat()
                except FileNotFoundError:
                    pass
                else:
                    return False
                return _directory_signature(root, identity) == proof["directory"]
    except (KeyError, OSError, TypeError, ValueError):
        return False
