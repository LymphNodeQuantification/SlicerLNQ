"""Chronicle-backed per-case review notes.

The reviewer's notes per case in the LNQ Worklist's Inference Review tab
persist as ReviewNote docs in Chronicle (CouchDB), keyed on the
cohort identifier + case_id. One doc per (cohort, case) keeps conflicts
isolated — two reviewers touching different cases in the same cohort
never step on each other.

Schema (untyped — Chronicle's validator only enforces shape on typed
docs, and treating review notes as a mutable scratchpad outside the
core schema means we don't have to write a new immutable version on
every keystroke):

    {
      "_id":        "reviewnote:<cohort_key>_<case_id>",
      "cohort_key": "<basename(data_root)>/<model_name>",
      "case_id":    "<case_id>",
      "notes":      "<text>",
      "updated_at": "<ISO-8601 UTC>",
      "updated_by": "<email>"
    }

Chronicle URL + admin password are read from one of:

    1. $CHRONICLE_CONF env var (path to a key=value file)
    2. ~/.lnq/chronicle.conf
    3. The SlicerLNQ-Chronicler checkout's chronicle.conf (sibling of
       this SlicerLNQ source tree — handy on the dev machine)

If no config is found OR the network is unreachable, write_note() is a
no-op + silently drops to in-memory only. We never block the reviewer
on a Chronicle outage; they keep editing locally and the next launch
just won't reflect what was lost.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def _candidate_conf_paths():
    """Where to look for chronicle.conf, in priority order."""
    out = []
    env_path = os.environ.get("CHRONICLE_CONF")
    if env_path:
        out.append(env_path)
    home = os.path.expanduser("~")
    out.append(os.path.join(home, ".lnq", "chronicle.conf"))
    # Try the sibling SlicerLNQ-Chronicler checkout — common dev layout.
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.normpath(os.path.join(
        here, "..", "..", "..", "SlicerLNQ-Chronicler", "chronicle.conf")))
    return out


def _load_conf():
    """Parse the first chronicle.conf we find. Returns a dict (empty if
    nothing's there)."""
    for path in _candidate_conf_paths():
        if not path or not os.path.isfile(path):
            continue
        out = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
        out["_conf_path"] = path
        return out
    return {}


def _safe_cohort_key(data_root, model_name):
    """Stable identifier for the cohort that survives moving the on-disk
    copy between machines. We use the data-root's basename + the model
    name. e.g. /Users/pieper/lnq-data/idc/ct_lymph_nodes +
    mediastinal-v1 → 'ct_lymph_nodes/mediastinal-v1'."""
    base = os.path.basename(os.path.normpath(data_root or "")) or "cohort"
    return f"{base}/{model_name or 'unknown'}"


def _doc_id(cohort_key, case_id):
    """Build a flat CouchDB doc id with no path separators. Slashes in
    the cohort key (e.g. 'ct_lymph_nodes/mediastinal-v1') would be
    interpreted by CouchDB as `/db/{docid}/{attname}` and silently
    routed to attachment storage — found that the hard way."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{cohort_key}_{case_id}")
    return f"reviewnote:{safe}"


class ChronicleNotesClient:
    """Thin urllib wrapper. All methods are no-ops + log.warning if the
    server is unreachable or unauthenticated — the worklist must never
    raise out of a typing slot."""

    def __init__(self, conf=None, actor=None):
        self._conf = conf or _load_conf()
        base = self._conf.get("CHRONICLE_URL")
        if not base and self._conf.get("DOMAIN_NAME"):
            base = "https://" + self._conf["DOMAIN_NAME"]
        self._base = (base or "").rstrip("/")
        self._db = self._conf.get("CHRONICLE_DB", "lnq")
        password = self._conf.get("COUCHDB_ADMIN_PASSWORD", "")
        if password:
            token = base64.b64encode(f"admin:{password}".encode()).decode()
            self._auth = f"Basic {token}"
        else:
            self._auth = None
        self._actor = actor or os.environ.get("USER") or "anonymous"

    @property
    def configured(self):
        return bool(self._base and self._auth)

    @property
    def base_url(self):
        return self._base

    def _req(self, method, doc_id, body=None, params=""):
        if not self.configured:
            return None
        # quote() with safe="" escapes ALL non-safe chars including the
        # colons in 'reviewnote:...' — CouchDB accepts encoded colons in
        # doc ids, so this is safer than letting them through raw.
        # _all_docs / _design / other system endpoints would *break* if
        # we quoted them, so keep underscores + leading-underscore paths
        # in the safe set.
        if doc_id.startswith("_"):
            quoted = doc_id
        else:
            quoted = urllib.parse.quote(doc_id, safe="")
        url = f"{self._base}/{self._db}/{quoted}{params}"
        data = json.dumps(body).encode() if body is not None else None
        # CouchDB's anti-CSRF rejects writes without a Referer header —
        # supply the same origin so all writes are accepted.
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": self._auth,
                     "Content-Type": "application/json",
                     "Referer": self._base + "/"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            try:
                detail = json.loads(e.read()).get("reason", "")
            except Exception:
                detail = ""
            logging.warning("Chronicle %s %s: HTTP %s %s",
                            method, doc_id, e.code, detail)
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            logging.warning("Chronicle %s %s: %s", method, doc_id, e)
            return None

    def fetch_notes(self, data_root, model_name, case_ids):
        """Return {case_id: notes_text} for every case in case_ids that
        has a stored ReviewNote. Missing cases are absent from the dict."""
        if not self.configured:
            return {}
        cohort_key = _safe_cohort_key(data_root, model_name)
        # CouchDB bulk-get via _all_docs?keys=[...]&include_docs=true.
        keys = [_doc_id(cohort_key, c) for c in case_ids]
        body = {"keys": keys}
        resp = self._req("POST", "_all_docs?include_docs=true", body=body)
        out = {}
        if not resp:
            return out
        for row in resp.get("rows", []):
            doc = row.get("doc") or {}
            cid = doc.get("case_id")
            text = doc.get("notes") or ""
            if cid:
                out[cid] = text
        return out

    def write_note(self, data_root, model_name, case_id, notes_text):
        """Upsert one ReviewNote doc. Returns True iff the round-trip
        succeeded."""
        if not self.configured:
            return False
        cohort_key = _safe_cohort_key(data_root, model_name)
        doc_id = _doc_id(cohort_key, case_id)
        existing = self._req("GET", doc_id) or {}
        now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        # Untyped doc — Chronicle's validate_doc_update.js returns early
        # on `!newDoc.type`, so we keep the doc out of the strict schema
        # (otherwise every keystroke would require a new immutable
        # version with a predecessor pointer).
        doc = {
            "_id": doc_id,
            "cohort_key": cohort_key,
            "case_id": case_id,
            "notes": notes_text,
            "updated_at": now,
            "updated_by": self._actor,
        }
        rev = existing.get("_rev")
        if rev:
            doc["_rev"] = rev
        resp = self._req("PUT", doc_id, body=doc)
        return bool(resp and resp.get("ok"))
