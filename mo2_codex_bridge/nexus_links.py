"""Parse public Nexus mod links and project public Nexus file metadata."""
from collections.abc import Mapping
from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlsplit


_HOSTS = {"nexusmods.com", "www.nexusmods.com"}
_GAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_MOD_PATH = re.compile(r"/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})/mods/([1-9][0-9]{0,9})/?")
_FILE_ANCHOR = re.compile(r"file-expander-header-([1-9][0-9]{0,9})")
_URL_ID = re.compile(r"[1-9][0-9]{0,9}")
_ATTRIBUTION = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}
_TABS = {"description", "files", "images", "videos", "posts", "bugs", "logs", "stats"}
_MAX_ID = 2_147_483_647  # MO2 uses signed int for Nexus IDs.


def _game_text(value):
    if not isinstance(value, str) or _GAME.fullmatch(value) is None:
        raise ValueError("MO2's managed game does not expose a supported Nexus identity")
    return value


def managed_nexus_game(managed_game):
    method = getattr(managed_game, "gameNexusName", None)
    return _game_text(method() if callable(method) else None).lower()


def _id(value):
    if type(value) is not int or not 0 < value <= _MAX_ID:
        raise ValueError("Nexus mod and file IDs must be positive integers within MO2's range")
    return value


def _url_id(value):
    if _URL_ID.fullmatch(value) is None:
        raise ValueError("The Nexus link does not contain a valid mod or file ID")
    return _id(int(value))


@dataclass(frozen=True)
class NexusLink:
    game: str
    mod_id: int
    file_id: int | None
    game_short: str

    def __post_init__(self):
        if _game_text(self.game).lower() != self.game:
            raise ValueError("A canonical Nexus game identifier is required")
        _game_text(self.game_short)
        _id(self.mod_id)
        if self.file_id is not None:
            _id(self.file_id)

    @property
    def files_url(self):
        return f"https://www.nexusmods.com/{self.game}/mods/{self.mod_id}?tab=files"

    def file_page(self, file_id):
        return f"{self.files_url}&file_id={_id(file_id)}"

    @property
    def file_url(self):
        return self.file_page(self.file_id) if self.file_id is not None else None

    @property
    def manual_download_url(self):
        file_url = self.file_url
        return f"{file_url}&nmm=1" if file_url is not None else None


def parse_nexus_link(url, managed_game, *, file_id=None):
    """Return the exact managed-game identity. No supplied URL is reused."""
    if (not isinstance(url, str) or len(url) > 2048
            or any(not 33 <= ord(char) <= 126 or char == "\\" for char in url)):
        raise ValueError("A public Nexus Mods HTTPS link is required")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError("A public Nexus Mods HTTPS link is required") from None
    match = _MOD_PATH.fullmatch(parts.path)
    # Comparing netloc also refuses userinfo, arbitrary ports and DNS aliases.
    if parts.scheme != "https" or parts.netloc.lower() not in _HOSTS or match is None:
        raise ValueError("Use a public Nexus Mods HTTPS mod or file page")
    game = managed_nexus_game(managed_game)
    if match[1].lower() != game:
        raise ValueError("This Nexus link is for a different game than the managed MO2 game")
    short_name = getattr(managed_game, "gameShortName", None)
    game_short = _game_text(short_name() if callable(short_name) else None)

    try:
        params = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                           errors="strict", max_num_fields=12)
    except ValueError:
        raise ValueError("The Nexus link has an invalid query") from None
    query = dict(params)
    if (len(query) != len(params)
            or set(query) - ({"tab", "file_id", "nmm", "jump_to_comment"} | _ATTRIBUTION)):
        raise ValueError("Use a Nexus mod or file page without duplicate or extra parameters")
    if "tab" in query and query["tab"] not in _TABS:
        raise ValueError("The Nexus link has an unsupported tab")
    if "jump_to_comment" in query and _URL_ID.fullmatch(query["jump_to_comment"]) is None:
        raise ValueError("The Nexus link has an invalid comment ID")
    ids = set()
    if file_id is not None:
        ids.add(_id(file_id))
    if "file_id" in query:
        if query.get("tab", "files") != "files":
            raise ValueError("A Nexus file link must point to the Files tab")
        ids.add(_url_id(query["file_id"]))
    if parts.fragment:
        anchor = _FILE_ANCHOR.fullmatch(parts.fragment)
        if anchor is None or query.get("tab") != "files":
            raise ValueError("Use a Nexus mod page or a link to an exact file")
        ids.add(_url_id(anchor[1]))
    if "nmm" in query and (query["nmm"] not in {"0", "1"} or not ids):
        raise ValueError("A Nexus download page must identify an exact file")
    if len(ids) > 1:
        raise ValueError("The requested Nexus file ID conflicts with the link")
    return NexusLink(game, _url_id(match[2]), next(iter(ids), None), game_short)


def _optional_text(value, limit):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.isprintable() or len(value) > limit:
        raise ValueError("Nexus supplied invalid public file metadata")
    return value


def _optional_number(value, maximum):
    if value is not None and (type(value) is not int or not 0 <= value <= maximum):
        raise ValueError("Nexus supplied an invalid file metadata number")
    return value


def normalize_nexus_file(raw, *, requested_file_id=None):
    """Return only whitelisted public data from MO2 or a public file catalog."""
    if not isinstance(raw, Mapping):
        raise ValueError("Nexus file metadata is unavailable")
    file_id = _id(raw.get("file_id"))
    if requested_file_id is not None and file_id != _id(requested_file_id):
        raise ValueError("Nexus returned a different file than requested")
    name = _optional_text(raw.get("name"), 512)
    filename = _optional_text(raw.get("file_name"), 255)
    if filename is not None and (filename[-1] in ". " or any(char in '\\/<>:"|?*' for char in filename)):
        raise ValueError("Nexus supplied a file name that cannot safely identify a local archive")
    version = _optional_text(raw.get("version"), 160)
    category = _optional_text(raw.get("category_name"), 64)
    primary = raw.get("is_primary")
    if primary is not None and type(primary) is not bool:
        raise ValueError("Nexus supplied an invalid primary-file marker")
    return {
        "file_id": file_id,
        "name": name,
        "file_name": filename,
        "version": version,
        "category_id": _optional_number(raw.get("category_id"), _MAX_ID),
        "category_name": category,
        "is_primary": primary,
        "size_in_bytes": _optional_number(raw.get("size_in_bytes"), 2**63 - 1),
        "uploaded_timestamp": _optional_number(raw.get("uploaded_timestamp"), 2**63 - 1),
    }


def normalize_nexus_files(payload, *, requested_file_id=None):
    if requested_file_id is not None:
        _id(requested_file_id)
    raw_files = payload.get("files") if isinstance(payload, Mapping) else None
    if not isinstance(raw_files, list) or len(raw_files) > 5000:
        raise ValueError("The Nexus file list is unavailable or exceeds the supported size")
    files = []
    by_id = {}
    for raw in raw_files:
        record = normalize_nexus_file(raw)
        file_id = record["file_id"]
        if file_id in by_id:
            raise ValueError("The Nexus file list contains an ambiguous file ID")
        by_id[file_id] = record
        files.append(record)
    if requested_file_id is not None and requested_file_id not in by_id:
        raise ValueError("The exact Nexus file was not found in the file list")
    return {"files": files, "selected_file": by_id.get(requested_file_id)}
