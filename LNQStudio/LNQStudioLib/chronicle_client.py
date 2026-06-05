"""HTTP client for the SlicerLNQ-Chronicle backend (CouchDB).

Mirrors the document discipline enforced by the server-side validator
(see SlicerLNQ-Chronicler/design/validate_doc_update.js): every typed
document carries `type`, `name`, `created_at`, `created_by`, `version`,
and `predecessor`; updates are *new documents* with `predecessor` linking
back to the previous version.

This module is deliberately Slicer-free. The Slicer module imports it,
but agents (cohort resolver, etc.) can use it too without dragging in Qt.
"""

import datetime
import hashlib
import json
import logging
import os
import socket
import uuid

import requests


TYPE_PREFIXES = {
    "Cohort": "cohort:",
    "Protocol": "protocol:",
    "Project": "project:",
    "CohortResolution": "cohortresolution:",
    "Annotation": "annotation:",
    "Blob": "blob:",
    "ModelGeneration": "modelgeneration:",
    "InferenceRun": "inferencerun:",
    "TrainingJob": "trainingjob:",
}

# Default local cache for blob bytes. Override by setting
# LNQ_CACHE_DIR or passing cache_dir= to ChronicleClient.
DEFAULT_CACHE_DIR = os.path.expanduser("~/.cache/lnq/blobs")

ANNOTATION_STATUSES = (
    "todo", "in_progress", "submitted_for_review",
    "needs_changes", "approved", "needs_consultation",
)


class ChronicleError(Exception):
    """Raised when the Chronicle returns a non-success HTTP response."""

    def __init__(self, status_code, reason, response_body=None):
        self.status_code = status_code
        self.reason = reason
        self.response_body = response_body
        super().__init__(f"HTTP {status_code}: {reason}")


def iso8601_now():
    """UTC timestamp, second precision, with explicit Z suffix."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(type_name):
    """Generate a doc ID of the form '<prefix><uuid4>'."""
    prefix = TYPE_PREFIXES[type_name]
    return f"{prefix}{uuid.uuid4()}"


class ChronicleChangesWatcher:
    """Background watcher of CouchDB's _changes feed in longpoll mode.

    Owns a daemon thread (NOT a QThread, so this module stays Slicer-free).
    The Slicer side connects to `on_changes` and pumps the callback through
    a qt.QTimer on the GUI thread when needed.

    Usage:
        w = ChronicleChangesWatcher(client, on_changes=lambda changes: ...)
        w.start()
        # ... time passes; on_changes fires each time CouchDB notifies ...
        w.stop()

    The callback runs on the watcher thread, so callers must marshal to the
    UI thread themselves. (LNQStudio's DashboardWindow uses qt.QMetaObject
    .invokeMethod with Qt.QueuedConnection.)
    """

    def __init__(self, client, on_changes, doc_types=None,
                 heartbeat_ms=10000, request_timeout_s=80):
        self._client = client
        self._on_changes = on_changes
        self._doc_types = set(doc_types) if doc_types else None
        self._heartbeat_ms = heartbeat_ms
        self._request_timeout_s = request_timeout_s
        self._stop_event = None
        self._thread = None
        self._since = "now"

    def start(self):
        import threading
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="LNQChronicleChanges")
        self._thread.start()

    def stop(self, timeout=5):
        if self._stop_event:
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self):
        import time
        while self._stop_event and not self._stop_event.is_set():
            try:
                r = self._client._session.get(
                    f"{self._client.base_url}/{self._client.db_name}/_changes",
                    params={
                        "feed": "longpoll",
                        "since": self._since,
                        "heartbeat": self._heartbeat_ms,
                        "include_docs": "true",
                    },
                    timeout=self._request_timeout_s,
                )
                if not r.ok:
                    logging.warning("changes feed HTTP %s: %s", r.status_code, r.text[:200])
                    if self._stop_event.wait(timeout=5):
                        return
                    continue
                data = r.json()
                self._since = data.get("last_seq", self._since)
                results = data.get("results") or []
                if self._doc_types and results:
                    filtered = []
                    for ch in results:
                        doc = ch.get("doc") or {}
                        if doc.get("type") in self._doc_types or doc.get("_deleted"):
                            filtered.append(ch)
                    results = filtered
                if results:
                    try:
                        self._on_changes(results)
                    except Exception:
                        logging.exception("changes callback raised")
            except requests.exceptions.Timeout:
                # heartbeat / request timeout — normal, reopen
                continue
            except Exception:
                logging.exception("changes feed read failed; backing off")
                if self._stop_event.wait(timeout=5):
                    return


class ChronicleClient:
    """Thin wrapper over the CouchDB HTTP API.

    Intentionally not async: the Slicer UI thread blocks on each call. For
    the LNQ data volumes (hundreds of cohorts, not millions) this is fine;
    if it becomes a problem we move to QThread or a worker process.
    """

    def __init__(self, base_url, username, password, db_name="lnq", timeout=15,
                 actor=None, cache_dir=None, manila_canonical=None, manila_local=None):
        """`username` + `password` authenticate the HTTP request to CouchDB.
        `actor` is the identity recorded on `created_by` and used for project
        membership matching. In Phase 2 (admin-party) these are different —
        you typically auth as `admin` but act as your real Slicer login. They
        collapse back into one when Phase 2a `_users` auth lands.

        `cache_dir`, `manila_canonical`, and `manila_local` configure blob
        resolution. The Slicer module passes its QSettings-derived values; the
        bin scripts can leave them None to get the defaults."""
        self.base_url = base_url.rstrip("/")
        self.db_name = db_name
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = (username, password)
        self._username = actor or username
        self.cache_dir = cache_dir or os.environ.get("LNQ_CACHE_DIR", DEFAULT_CACHE_DIR)
        self.manila_canonical = manila_canonical or "/media/share/LNQ-data"
        self.manila_local = manila_local or "/private/tmp/media/share/LNQ-data"
        self.host_label = socket.gethostname()

    # ----- low-level -----

    def _url(self, *parts):
        return "/".join([self.base_url, self.db_name, *parts])

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        response = self._session.request(method, url, **kwargs)
        if not response.ok:
            try:
                body = response.json()
                reason = body.get("reason") or body.get("error") or response.text
            except ValueError:
                body = None
                reason = response.text or response.reason
            raise ChronicleError(response.status_code, reason, body)
        return response

    # ----- server / database probes -----

    def ping(self):
        """Returns CouchDB's /_up payload. Raises ChronicleError on failure."""
        response = self._request("GET", f"{self.base_url}/_up")
        return response.json()

    def server_info(self):
        """Returns CouchDB's root payload (version, etc.)."""
        response = self._request("GET", self.base_url)
        return response.json()

    def database_exists(self):
        try:
            self._request("HEAD", self._url())
            return True
        except ChronicleError as exc:
            if exc.status_code == 404:
                return False
            raise

    # ----- document operations -----

    def get(self, doc_id):
        return self._request("GET", self._url(doc_id)).json()

    def put(self, doc):
        return self._request(
            "PUT",
            self._url(doc["_id"]),
            headers={"Content-Type": "application/json"},
            data=json.dumps(doc),
        ).json()

    def list_by_type(self, type_name):
        """Return all current documents of a given type, sorted by _id.

        Uses CouchDB's _all_docs with a startkey/endkey range over the type
        prefix. For phase-2 sizes this is fine; later we'll back it with a
        view (and/or filter to "current version only" via Mango).
        """
        prefix = TYPE_PREFIXES[type_name]
        response = self._request(
            "GET",
            self._url("_all_docs"),
            params={
                "startkey": json.dumps(prefix),
                "endkey": json.dumps(prefix + "￰"),
                "include_docs": "true",
            },
        )
        rows = response.json().get("rows", [])
        return [row["doc"] for row in rows if row.get("doc") and not row["id"].startswith("_design")]

    @staticmethod
    def head_of_chain(docs):
        """Filter `docs` to just the heads — docs whose _id isn't anyone else's
        `predecessor`. Chronicle's immutable-document model means renames and
        edits produce new version docs that point back via `predecessor`; the
        UI almost always wants only the latest version of each conceptual doc."""
        superseded = {d.get("predecessor") for d in docs if d.get("predecessor")}
        return [d for d in docs if d.get("_id") not in superseded]

    def list_heads_by_type(self, type_name):
        """Like list_by_type but filtered to chain heads. Use this for any
        worklist / picker UI that should show one entry per conceptual doc."""
        return self.head_of_chain(self.list_by_type(type_name))

    # ----- typed creation helpers -----

    def _base_doc(self, type_name, name, predecessor=None, version=1, doc_id=None):
        return {
            "_id": doc_id or new_id(type_name),
            "type": type_name,
            "name": name,
            "created_at": iso8601_now(),
            "created_by": self._username,
            "version": version,
            "predecessor": predecessor,
        }

    def create_cohort(self, name, sources, description=None, consort=None):
        doc = self._base_doc("Cohort", name)
        doc["sources"] = sources
        if description is not None:
            doc["description"] = description
        if consort is not None:
            doc["consort"] = consort
        return self.put(doc)

    def create_protocol(self, name, color_table, description=None, rules=None):
        doc = self._base_doc("Protocol", name)
        doc["color_table"] = color_table
        if description is not None:
            doc["description"] = description
        if rules is not None:
            doc["rules"] = rules
        return self.put(doc)

    def create_project(self, name, cohort_id, protocol_id, members, description=None):
        doc = self._base_doc("Project", name)
        doc["cohort_id"] = cohort_id
        doc["protocol_id"] = protocol_id
        doc["members"] = members
        if description is not None:
            doc["description"] = description
        return self.put(doc)

    def create_annotation(self, project_id, case_id, status, notes,
                          predecessor=None, version=None, study_uid=None,
                          seg_ref=None, producer=None):
        """Create a new Annotation document. If `predecessor` is given,
        `version` is inferred as predecessor.version + 1; otherwise version=1.

        producer defaults to {"kind": "review", "label": "in-slicer"}, suitable
        for a status-only update done from the Slicer module.
        """
        if predecessor is not None and version is None:
            # predecessor may be either an _id string or the full doc
            if isinstance(predecessor, dict):
                pred_version = predecessor.get("version", 1)
                pred_id = predecessor["_id"]
            else:
                pred_doc = self.get(predecessor)
                pred_version = pred_doc.get("version", 1)
                pred_id = pred_doc["_id"]
            version = pred_version + 1
            predecessor_id = pred_id
        else:
            version = version or 1
            predecessor_id = None
        doc = self._base_doc(
            "Annotation",
            f"{case_id} / {(producer or {}).get('label') or 'review'}",
            predecessor=predecessor_id,
            version=version,
        )
        doc.update({
            "project_id": project_id,
            "case_id": case_id,
            "study_uid": study_uid,
            "status": status,
            "notes": notes or "",
            "producer": producer or {"kind": "review", "label": "in-slicer", "model_generation_id": None},
            "seg_ref": seg_ref,
        })
        return self.put(doc)

    # ----- TrainingJob helpers -----

    def list_training_jobs(self, project_id=None):
        jobs = self.list_by_type("TrainingJob")
        if project_id:
            jobs = [j for j in jobs if j.get("project_id") == project_id]
        jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
        return jobs

    def create_training_job(self, project_id, label, training_annotation_ids,
                            config, status="pending", host=None, notes=""):
        """Create a fresh TrainingJob in 'pending' state. Trainer updates the
        status/heartbeat/metrics fields in place as it runs."""
        doc_id = f"trainingjob:{uuid.uuid4()}"
        doc = {
            "_id": doc_id,
            "type": "TrainingJob",
            "name": f"{label} / fold {config.get('fold')}",
            "created_at": iso8601_now(),
            "created_by": self._username,
            "version": 1,
            "predecessor": None,
            "project_id": project_id,
            "label": label,
            "training_annotation_ids": list(training_annotation_ids),
            "config": config,
            "status": status,
            "started_at": None,
            "finished_at": None,
            "last_heartbeat_at": None,
            "current_epoch": None,
            "latest_metrics": None,
            "host": host,
            "log_ref": None,
            "model_generation_id": None,
            "notes": notes or "",
        }
        self.put(doc)
        return doc

    # ----- blob index + resolver -----

    def _cache_dir_for(self, sha256):
        # Two-level fan-out so the directory doesn't blow up with one giant flat list.
        return os.path.join(self.cache_dir, sha256[:2])

    def _cache_path_with_ext(self, sha256, ext):
        return os.path.join(self._cache_dir_for(sha256), sha256 + (ext or ""))

    def _cached_for(self, sha256):
        """Return the cache path for sha256 if any cached copy exists (any
        extension), else None."""
        leaf = self._cache_dir_for(sha256)
        if not os.path.isdir(leaf):
            return None
        for name in os.listdir(leaf):
            if name.startswith(sha256):
                return os.path.join(leaf, name)
        return None

    @staticmethod
    def _ext_of(path):
        """Extension including dot. Recognises compound suffixes (.seg.nrrd,
        .nii.gz) so Slicer's readers pick the right loader."""
        low = path.lower()
        for combined in (".seg.nrrd", ".nii.gz"):
            if low.endswith(combined):
                return path[-len(combined):]
        return os.path.splitext(path)[1]

    def _manila_rewrite(self, path):
        """If `path` looks like a Manila-canonical absolute path and the
        local-mount equivalent exists, return that. Otherwise return path."""
        c, l = self.manila_canonical, self.manila_local
        if c and l and path.startswith(c):
            candidate = l + path[len(c):]
            if os.path.lexists(candidate):
                return candidate
        return path

    def _resolve_symlinks(self, path):
        """Walk symlinks manually, applying Manila rewrite at each hop."""
        visited = set()
        current = path
        while True:
            if current in visited:
                return current
            visited.add(current)
            if not os.path.islink(current):
                return current
            target = os.readlink(current)
            if not os.path.isabs(target):
                target = os.path.normpath(os.path.join(os.path.dirname(current), target))
            current = self._manila_rewrite(target)

    def _local_path_from_uri(self, value):
        """Strip file://, apply Manila override, then resolve symlinks."""
        if value.startswith("file://"):
            value = value[len("file://"):]
        value = self._manila_rewrite(value)
        if os.path.lexists(value):
            value = self._resolve_symlinks(value)
        return value

    @staticmethod
    def hash_file(path, chunk_size=4 * 1024 * 1024):
        """Stream-hash a file. Returns (sha256_hex, size_bytes)."""
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    def register_blob(self, local_path, mime_type=None, host=None, add_to_cache=True):
        """Hash `local_path`, create or update a Blob doc with its location,
        optionally hardlink/copy it into the local cache. Returns the Blob doc.

        Idempotent: rerunning on the same file is a no-op except for refreshing
        verified_at on the matching location."""
        sha256, size = self.hash_file(local_path)
        blob_id = f"blob:{sha256}"
        host_label = host or self.host_label
        location = {
            "kind": "local-uri",
            "value": f"file://{os.path.abspath(local_path)}",
            "host": host_label,
            "verified_at": iso8601_now(),
        }
        # Look for an existing Blob.
        try:
            existing = self.get(blob_id)
            # Replace any matching (kind,value) location, else append.
            locs = list(existing.get("locations") or [])
            replaced = False
            for i, l in enumerate(locs):
                if l.get("kind") == location["kind"] and l.get("value") == location["value"]:
                    locs[i] = location
                    replaced = True
                    break
            if not replaced:
                locs.append(location)
            existing["locations"] = locs
            blob_doc = self.put(existing) and existing
        except ChronicleError as exc:
            if exc.status_code != 404:
                raise
            blob_doc = {
                "_id": blob_id,
                "type": "Blob",
                "name": f"{sha256[:8]}… ({size} bytes)",
                "created_at": iso8601_now(),
                "created_by": self._username,
                "version": 1,
                "predecessor": None,
                "sha256": sha256,
                "size": size,
                "mime_type": mime_type or self._guess_mime(local_path),
                "locations": [location],
            }
            self.put(blob_doc)

        if add_to_cache:
            ext = self._ext_of(local_path)
            self._ensure_cached_at(local_path, self._cache_path_with_ext(sha256, ext))
        return blob_doc

    @staticmethod
    def _guess_mime(path):
        low = path.lower()
        if low.endswith(".seg.nrrd"):
            return "application/octet-stream+seg.nrrd"
        if low.endswith(".nrrd"):
            return "application/octet-stream+nrrd"
        if low.endswith(".nii") or low.endswith(".nii.gz"):
            return "application/octet-stream+nifti"
        return None

    def _ensure_cached_at(self, src_path, dst):
        """Place a cache entry at `dst` pointing at the bytes at `src_path`.
        Hardlink if possible (cheap, atomic, shares inodes); fall back to
        symlink across filesystem boundaries."""
        if os.path.lexists(dst):
            return dst
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            os.link(src_path, dst)
        except OSError:
            try:
                os.symlink(os.path.abspath(src_path), dst)
            except OSError as exc:
                logging.warning("could not cache %s: %s", src_path, exc)
                return None
        return dst

    def resolve_blob(self, blob_id, verify=False):
        """Return a local filesystem path for a Blob, fetching from a known
        location into cache if necessary. Returns None if no location is
        reachable. `verify=True` re-hashes the result before returning."""
        sha256 = blob_id.removeprefix("blob:")
        cached = self._cached_for(sha256)
        if cached:
            if not verify:
                return cached
            actual, _ = self.hash_file(cached)
            if actual == sha256:
                return cached
            logging.warning("cache hash mismatch for %s; refetching", blob_id)
            try:
                os.remove(cached)
            except OSError:
                pass
        try:
            blob = self.get(blob_id)
        except ChronicleError:
            return None
        for loc in blob.get("locations") or []:
            if loc.get("kind") == "local-uri" and loc.get("value"):
                p = self._local_path_from_uri(loc["value"])
                if os.path.exists(p):
                    ext = self._ext_of(loc["value"])
                    dst = self._cache_path_with_ext(sha256, ext)
                    self._ensure_cached_at(p, dst)
                    return dst if os.path.lexists(dst) else p
        # TODO: swift / https fetch when those locations actually exist.
        return None

    def resolve_ref(self, ref):
        """Return a local path for either shape of file reference:
          - new: {"blob_id": "blob:..."}
          - legacy: {"kind": "local-uri", "value": "file://...", "sha256": ..., "size": ...}
        Returns None if the reference can't be resolved."""
        if not ref:
            return None
        if ref.get("blob_id"):
            return self.resolve_blob(ref["blob_id"])
        if ref.get("kind") == "local-uri" and ref.get("value"):
            p = self._local_path_from_uri(ref["value"])
            if os.path.exists(p):
                return p
        return None

    # ----- worklist helpers -----

    def latest_cohort_resolution(self, cohort_id):
        """Return the most recent CohortResolution doc for `cohort_id`,
        walking both the cohort's predecessor chain (so a v2 cohort inherits
        its v1's resolution) and the resolution's own predecessor chain (so
        a resolution that was superseded by a v2 doesn't get returned)."""
        cohort_chain = self.chain_ids(cohort_id)
        resolutions = [r for r in self.list_by_type("CohortResolution")
                       if r.get("cohort_id") in cohort_chain]
        if not resolutions:
            return None
        # Filter to heads-of-chain — drop any resolution that's been
        # superseded by a newer version.
        superseded = {r.get("predecessor") for r in resolutions if r.get("predecessor")}
        heads = [r for r in resolutions if r["_id"] not in superseded]
        if not heads:
            heads = resolutions  # fallback if everything is somehow superseded
        heads.sort(key=lambda r: (r.get("resolved_at") or r.get("created_at") or ""))
        return heads[-1]

    def chain_ids(self, doc_id):
        """Walk a typed doc's predecessor chain backward, returning the set of
        all doc_ids in it (the given head + every ancestor). Works for any
        chronicle doc type that uses the `predecessor: <doc_id>` convention
        (Project, Cohort, Protocol, Annotation)."""
        chain = set()
        current = doc_id
        while current and current not in chain:
            chain.add(current)
            try:
                doc = self.get(current)
            except ChronicleError:
                break
            current = doc.get("predecessor")
        return chain

    def project_chain_ids(self, project_id):
        """Backwards-compatible alias for `chain_ids`. Use chain_ids() for
        new code so it's obvious the helper is type-agnostic."""
        return self.chain_ids(project_id)

    def annotations_for_project(self, project_id):
        """Return all Annotation docs belonging to `project_id`'s chain
        (the head plus any predecessor versions of the project), sorted by
        (case_id, version).

        Chronicle's immutable-doc model means a renamed-or-revised project
        gets a new doc with `predecessor: <old_id>`. Annotations stay
        attached to the project_id they were written against, so the v2
        head needs to also surface the v1 cohort's annotations."""
        chain = self.project_chain_ids(project_id)
        anns = [a for a in self.list_by_type("Annotation")
                if a.get("project_id") in chain]
        anns.sort(key=lambda a: (a.get("case_id") or "", a.get("version") or 0))
        return anns

    def annotation_chains_by_case(self, project_id):
        """Return {case_id: [annotation_v1, annotation_v2, ...]} for the
        project. Each list is ordered oldest-to-newest by version."""
        chains = {}
        for ann in self.annotations_for_project(project_id):
            chains.setdefault(ann["case_id"], []).append(ann)
        return chains

    # ----- new versions of existing docs -----

    def new_version(self, predecessor_doc, **field_overrides):
        """Build (but do not write) a new-version doc that supersedes predecessor_doc.

        Copies type-specific fields from the predecessor, bumps version, sets
        predecessor pointer, regenerates _id and timestamps. Caller passes
        keyword overrides for whatever fields they're changing.
        """
        type_name = predecessor_doc["type"]
        new_doc = self._base_doc(
            type_name,
            field_overrides.pop("name", predecessor_doc.get("name")),
            predecessor=predecessor_doc["_id"],
            version=predecessor_doc["version"] + 1,
        )
        # Carry forward type-specific fields, then apply overrides.
        for key, value in predecessor_doc.items():
            if key in ("_id", "_rev", "type", "name", "created_at", "created_by",
                      "version", "predecessor"):
                continue
            new_doc[key] = value
        new_doc.update(field_overrides)
        return new_doc
