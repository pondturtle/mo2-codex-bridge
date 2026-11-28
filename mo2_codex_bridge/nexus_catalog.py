"""Public file metadata via the official Nexus GraphQL API."""
from email.utils import format_datetime, parsedate_to_datetime
import http.client
import json
import math
import re
import socket
from threading import Timer
from time import monotonic, time

from .common import VERSION
from .nexus_links import _game_text, _id


_HOST = "api.nexusmods.com"
_PATH = "/v2/graphql"
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 5000
_TIMEOUT = 12
_NUMERIC_TEXT = re.compile(r"[0-9]{1,19}")
_BACKOFF_STATUSES = {429, 503}

# https://graphql.nexusmods.com/#query-game
_GAME_QUERY = """
query BridgeGame($domain: String!) {
  game(domainName: $domain) { id domainName }
}
"""
# https://graphql.nexusmods.com/#query-modFiles
# No URL, description, viewer, or account fields are requested.
_FILES_QUERY = """
query BridgeFiles($gameId: ID!, $modId: ID!) {
  mod(gameId: $gameId, modId: $modId) {
    modId adultContent directDownloadEnabled game { id domainName }
  }
  modFiles(gameId: $gameId, modId: $modId) {
    modId game { id domainName } fileId name version category categoryId
    primary sizeInBytes date
  }
}
"""


class PublicCatalogError(ValueError):
    """The requested public Nexus catalog could not be verified."""


class PublicCatalogBackoff(PublicCatalogError):
    """Nexus returned a valid wait for a later request."""

    def __init__(self, retry_after_seconds):
        if type(retry_after_seconds) is not int or retry_after_seconds < 0:
            raise ValueError("Invalid Nexus catalog wait")
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Nexus asked the bridge to wait before requesting its public file catalog again")


def _http_date(value):
    # Accept the canonical HTTP date, with UTC and a matching weekday.
    if len(value) != 29:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None or format_datetime(parsed, usegmt=True) != value:
            return None
        return parsed.timestamp()
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _retry_after(response):
    headers = response.getheaders()
    values = [value.strip(" \t") for name, value in headers if name.casefold() == "retry-after"]
    if len(values) != 1:
        return None
    retry_after = values[0]
    if _NUMERIC_TEXT.fullmatch(retry_after):
        return int(retry_after)
    deadline = _http_date(retry_after)
    if deadline is None:
        return None
    dates = [value.strip(" \t") for name, value in headers if name.casefold() == "date"]
    server_clock = _http_date(dates[0]) if len(dates) == 1 else None
    # The server's Date can protect the requested interval from a fast local clock.
    earliest_clock = min(time(), server_clock) if server_clock is not None else time()
    return max(0, math.ceil(deadline - earliest_clock))


def _remaining(deadline):
    allowance = deadline - monotonic()
    if allowance <= 0:
        raise PublicCatalogError("The Nexus file catalog request timed out")
    return allowance


def _interrupt(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PublicCatalogError("Nexus returned an ambiguous catalog response")
        result[key] = value
    return result


def _nonfinite(_):
    raise PublicCatalogError("Nexus returned an invalid catalog number")


def _graph(query, variables, deadline):
    """POST a fixed query to a TLS-verified peer; leave redirects to the user."""
    transport = getattr(http.client, "HTTPSConnection", None)
    if transport is None:
        raise PublicCatalogError("HTTPS support is unavailable for the Nexus file catalog")
    body = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/graphql-response+json, application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "Application-Name": "MO2 Codex Bridge",
        "Application-Version": VERSION,
    }
    connection = transport(_HOST, timeout=_remaining(deadline))
    stop = None
    try:
        connection.connect()
        # A peer trickling a response must not extend the request deadline.
        stop = Timer(_remaining(deadline), _interrupt, args=(connection.sock,))
        stop.daemon = True
        stop.start()
        connection.request("POST", _PATH, body, headers)
        with connection.getresponse() as response:
            if response.status != 200:
                if response.status in _BACKOFF_STATUSES:
                    delay = _retry_after(response)
                    if delay is not None:
                        raise PublicCatalogBackoff(delay)
                raise PublicCatalogError("Nexus did not return a public file catalog")
            mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            encoding = response.getheader("Content-Encoding", "identity").lower()
            if mime not in {"application/graphql-response+json", "application/json"} or encoding != "identity":
                raise PublicCatalogError("Nexus returned an unsupported catalog response")
            length = response.getheader("Content-Length")
            if length is not None and (_NUMERIC_TEXT.fullmatch(length) is None or int(length) > _MAX_BYTES):
                raise PublicCatalogError("The Nexus catalog response exceeded the supported size")
            raw = response.read(_MAX_BYTES + 1)
        _remaining(deadline)
        if not raw or len(raw) > _MAX_BYTES or (length is not None and len(raw) != int(length)):
            raise PublicCatalogError("The Nexus catalog response was incomplete or too large")
        result = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    except PublicCatalogError:
        raise
    except (OSError, http.client.HTTPException, ValueError, RecursionError):
        raise PublicCatalogError("The public Nexus file catalog could not be read") from None
    finally:
        if stop is not None:
            stop.cancel()
        connection.close()
    if (not isinstance(result, dict) or "errors" in result
            or not isinstance(result.get("data"), dict)):
        raise PublicCatalogError("Nexus did not return a verifiable public file catalog")
    return result["data"]


def _numeric(value):
    # GraphQL ID and BigInt scalars may be serialized as decimal strings.
    return int(value) if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value) else value


def _bound_game(row, domain):
    if not isinstance(row, dict) or row.get("domainName") != domain:
        raise PublicCatalogError("Nexus returned a different game than requested")
    try:
        return _id(_numeric(row.get("id")))
    except ValueError:
        raise PublicCatalogError("Nexus returned an invalid game identity") from None


def fetch_public_files(game, mod_id):
    """Return enough public metadata to choose an exact file. Makes two queries."""
    if _game_text(game).lower() != game:
        raise ValueError("A canonical Nexus game identifier is required")
    _id(mod_id)
    deadline = monotonic() + _TIMEOUT
    game_data = _graph(_GAME_QUERY, {"domain": game}, deadline)
    game_id = _bound_game(game_data.get("game"), game)
    data = _graph(_FILES_QUERY, {"gameId": str(game_id), "modId": str(mod_id)}, deadline)
    mod = data.get("mod")
    if (not isinstance(mod, dict) or type(mod.get("modId")) is not int
            or mod["modId"] != mod_id or _bound_game(mod.get("game"), game) != game_id):
        raise PublicCatalogError("Nexus returned a different mod than requested")
    if mod.get("adultContent") is not False:
        raise PublicCatalogError("Use the Nexus Files page for this restricted or unclassified mod")
    direct_download = mod.get("directDownloadEnabled")
    if type(direct_download) is not bool:
        raise PublicCatalogError("Nexus returned an invalid mod download setting")
    source = data.get("modFiles")
    if not isinstance(source, list) or len(source) > _MAX_ROWS:
        raise PublicCatalogError("The Nexus catalog was unavailable or too large")
    files = []
    for raw in source:
        if (not isinstance(raw, dict) or type(raw.get("modId")) is not int
                or raw["modId"] != mod_id or _bound_game(raw.get("game"), game) != game_id):
            raise PublicCatalogError("Nexus returned a file from a different game or mod")
        primary = raw.get("primary")
        if type(primary) is not int or primary not in {0, 1}:
            raise PublicCatalogError("Nexus returned an invalid primary-file marker")
        files.append({
            "file_id": raw.get("fileId"),
            "name": raw.get("name"),
            "version": raw.get("version"),
            "category_id": raw.get("categoryId"),
            "category_name": raw.get("category"),
            "is_primary": bool(primary),
            "size_in_bytes": _numeric(raw.get("sizeInBytes")),
            "uploaded_timestamp": raw.get("date"),
        })
    return {"files": files, "direct_download_enabled": direct_download}
