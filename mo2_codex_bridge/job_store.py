"""Bounded in-memory results with durable, non-expiring mutation deduplication.

Read receipts expire. A mutation claim is durable BEFORE enqueue and never
automatically deleted. Incomplete claims survive restart as recovery-required,
never as executable work. The on-disk ledger is intentionally not RAM bounded.
"""
import hashlib
import json
import os
import threading
import time
from collections.abc import MutableMapping
from pathlib import Path

from .common import atomic_write, contained_path

TERMINAL = {"done", "failed"}
READ_LIMIT = 128
MUTATION_LIMIT = 64
READ_TTL = 300
MUTATION_TTL = 1800
CACHE_BYTES = 8 * 1024 * 1024
RECEIPT_BYTES = 128 * 1024


def request_fingerprint(action, params):
    encoded = json.dumps({"action": action, "params": params}, sort_keys=True,
                         ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def public_job(job):
    return {key: value for key, value in job.items()
            if key not in {"params", "created", "finished"} and not key.startswith("_")}


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate mutation receipt metadata key")
        result[key] = value
    return result


class JobStore(MutableMapping):
    def __init__(self, data, initial=None, *, clock=time.monotonic, read_limit=READ_LIMIT,
                 mutation_limit=MUTATION_LIMIT, byte_limit=CACHE_BYTES,
                 read_ttl=READ_TTL, mutation_ttl=MUTATION_TTL):
        self.root = Path(data) / "mutation-receipts"
        self.cache = dict(initial or {})
        self.clock = clock
        self.read_limit, self.mutation_limit = read_limit, mutation_limit
        self.byte_limit, self.read_ttl, self.mutation_ttl = byte_limit, read_ttl, mutation_ttl
        self.lock = threading.RLock()

    def __getitem__(self, key):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.cache[key] = value

    def __delitem__(self, key):
        # Removing a cache entry never removes a durable mutation identity.
        del self.cache[key]

    def __iter__(self):
        return iter(self.cache)

    def __len__(self):
        return len(self.cache)

    def _file(self, job_id):
        if not isinstance(job_id, str) or not job_id or len(job_id) > 80:
            raise ValueError("Invalid request ID")
        if self.root.is_symlink() or (hasattr(self.root, "is_junction") and self.root.is_junction()):
            raise ValueError("Linked mutation receipt store refused")
        key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return contained_path(self.root, key[:2] + "/" + key + ".json")

    def _read(self, job_id):
        path = self._file(job_id)
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > RECEIPT_BYTES:
            raise ValueError("Mutation receipt unreadable or too large; refusing request ID reuse")
        with path.open("rb") as stream:
            raw = stream.read(RECEIPT_BYTES + 1)
        if len(raw) > RECEIPT_BYTES:
            raise ValueError("Mutation receipt exceeds its size limit")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
        if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1 or value.get("job_id") != job_id
                or not isinstance(value.get("fingerprint"), str) or len(value["fingerprint"]) != 64
                or any(c not in "0123456789abcdef" for c in value["fingerprint"])
                or not isinstance(value.get("action"), str)
                or value.get("state") not in {"claimed", "terminal"}):
            raise ValueError("Mutation receipt identity is invalid; refusing request ID reuse")
        if value["state"] == "terminal":
            outcome = value.get("outcome")
            if (not isinstance(outcome, dict) or outcome.get("job_id") != job_id
                    or outcome.get("action") != value["action"] or outcome.get("status") not in TERMINAL):
                raise ValueError("Mutation receipt outcome is invalid; refusing request ID reuse")
        return value

    def get(self, job_id, default=None):
        with self.lock:
            self.prune()
            if job_id in self.cache:
                return self.cache[job_id]
            receipt = self._read(job_id)
            if receipt is None:
                return default
            if receipt["state"] == "terminal":
                return dict(receipt["outcome"], _fingerprint=receipt["fingerprint"], _mutation=True)
            # No running job is recreated after a restart. New clients surface
            # this unknown state before attempting any installer interaction.
            recovery = receipt.get("recovery", {})
            return {**recovery, "job_id": job_id, "action": receipt["action"], "status": "unknown",
                    "pending": True, "recovery_required": True,
                    "message": "This mutation has no durable terminal receipt. Inspect recovery backups and audit; do not resubmit.",
                    "_fingerprint": receipt["fingerprint"], "_mutation": True}

    def lookup(self, job_id, action, params):
        with self.lock:
            previous = self.get(job_id)
            if previous is not None:
                fingerprint = previous.get("_fingerprint") or request_fingerprint(previous["action"], previous["params"])
                if fingerprint != request_fingerprint(action, params):
                    raise ValueError("Request ID already belongs to another operation")
            return previous

    def reserve(self, job, *, mutation):
        with self.lock:
            if self.lookup(job["job_id"], job["action"], job["params"]) is not None:
                raise ValueError("Request ID already reserved")
            job["_fingerprint"] = request_fingerprint(job["action"], job["params"])
            job["_mutation"] = mutation
            if mutation:
                claim = {"schema": 1, "job_id": job["job_id"], "action": job["action"],
                         "fingerprint": job["_fingerprint"], "state": "claimed"}
                path = self._file(job["job_id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                raw = json.dumps(claim, sort_keys=True).encode("utf-8")
                with path.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.read_bytes() != raw:
                    raise OSError("Mutation claim readback failed; no work enqueued")
            self.cache[job["job_id"]] = job

    def complete(self, job):
        with self.lock:
            if job.get("status") not in TERMINAL:
                return
            job["finished"] = self.clock()
            if job.get("_mutation"):
                try:
                    prior = self._read(job["job_id"])
                    if prior is None or prior["fingerprint"] != job["_fingerprint"]:
                        raise ValueError("Mutation claim changed before completion")
                    outcome = public_job(job)
                    final = dict(prior, state="terminal", outcome=outcome)
                    raw = json.dumps(final, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    if len(raw) > RECEIPT_BYTES:
                        # Preserve non-reexecution and recovery linkage even for
                        # unexpectedly huge operation results.
                        details = job.get("failure", {})
                        result = job.get("result", {})
                        summary = {"job_id": job["job_id"], "action": job["action"],
                                   "status": "unknown", "pending": True, "recovery_required": True,
                                   "backup_id": details.get("backup_id") or result.get("backup_id"),
                                   "message": "Terminal result exceeded receipt size limit; inspect backup result/error journal. Do not resubmit."}
                        # Keep a claimed state with observed recovery metadata.
                        final = dict(prior, recovery=summary)
                        raw = json.dumps(final, sort_keys=True).encode("utf-8")
                    def guard():
                        current = self._read(job["job_id"])
                        if current != prior:
                            raise ValueError("Mutation claim changed during receipt staging")
                    atomic_write(self._file(job["job_id"]), raw, before_replace=guard)
                    job["receipt_recorded"] = True
                except Exception as error:
                    # The initial durable claim still prevents reexecution.
                    job.update(receipt_recorded=False, receipt_error=f"{type(error).__name__}: {error}")
                    if isinstance(job.get("failure"), dict):
                        job["failure"].update(receipt_recorded=False, receipt_error=job["receipt_error"])
                    if isinstance(job.get("result"), dict):
                        job["result"] = dict(job["result"], receipt_recorded=False, receipt_error=job["receipt_error"])
            job["_retained_size"] = len(json.dumps(job, ensure_ascii=False, default=str).encode("utf-8"))
            if job["_retained_size"] > self.byte_limit and not job.get("_mutation"):
                job.pop("result", None)
                job.update(status="failed", error="Read result exceeds retention limit",
                           failure={"message": "Read result exceeds bounded retention limit; request a narrower view",
                                    "error_type": "ResultRetentionLimit", "job_id": job["job_id"],
                                    "pending": False, "changes_may_have_occurred": False})
                job["_retained_size"] = len(json.dumps(job, ensure_ascii=False).encode("utf-8"))
            self.prune()

    def prune(self):
        now = self.clock()
        reads, mutations = [], []
        for key, job in list(self.cache.items()):
            if job.get("status") not in TERMINAL:
                continue
            if "_retained_size" not in job:
                job["_retained_size"] = len(json.dumps(job, ensure_ascii=False, default=str).encode("utf-8"))
            mutation = job.get("_mutation", False)
            finished = job.get("finished", job.get("created", now))
            ttl = self.mutation_ttl if mutation else self.read_ttl
            if now - finished > ttl:
                del self.cache[key]
            else:
                (mutations if mutation else reads).append((finished, key))
        for group, limit in ((reads, self.read_limit), (mutations, self.mutation_limit)):
            for _, key in sorted(group)[:max(0, len(group) - limit)]:
                self.cache.pop(key, None)
        terminals = sorted((job.get("finished", now), key) for key, job in self.cache.items()
                           if job.get("status") in TERMINAL)
        total = sum(self.cache[key].get("_retained_size", 0) for _, key in terminals)
        for _, key in terminals:
            if total <= self.byte_limit:
                break
            job = self.cache.pop(key)
            total -= job.get("_retained_size", 0)

    def describe(self):
        return {"terminal_read_limit": self.read_limit, "terminal_mutation_limit": self.mutation_limit,
                "terminal_cache_bytes": self.byte_limit, "read_receipt_seconds": self.read_ttl,
                "active_jobs_evicted": False, "mutation_id_retention": "durable; no automatic expiry",
                "interrupted_mutation_policy": "recovery-required; never re-enqueued"}
