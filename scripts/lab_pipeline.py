#!/usr/bin/env python3
"""Local identity boundary for lab-grader. CLI roots are fixed relative to this file.

Only fixed status codes and counts may reach stdout/stderr. Never import this
module into an agent/browser REPL to inspect its private return values.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import html
from html.parser import HTMLParser
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit
import uuid
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 40 * 1024 * 1024
KEY_RE = re.compile(r"S-[0-9a-f]{32}\Z")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")


class Stop(Exception):
    """Arguments are fixed codes, never data, paths, HTTP bodies, or identifiers."""


def require(condition, code):
    if not condition:
        raise Stop(code)


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False,
                           allow_nan=False, separators=(",", ":")).encode()
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def no_links(path):
    require(not any(p.is_symlink() for p in [path, *path.parents]), "SYMLINK_REFUSED")


def save(path, value):
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = value if isinstance(value, bytes) else json.dumps(
        value, indent=2, ensure_ascii=False, allow_nan=False).encode()
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def lock(path):
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as stream:
        path.chmod(0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop("BUSY") from None
        yield


def number(value):
    require(type(value) in (int, float) and math.isfinite(value), "INVALID_POINTS")
    return float(value)


def identifier(value):
    require(re.fullmatch(r"[0-9]+", str(value)) is not None, "INVALID_API_ID")
    return str(value)


def normalize(text):
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden, self.has_media = [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("img", "svg", "video", "audio", "iframe", "object", "canvas"):
            self.has_media = True
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_html(text):
    parser = PlainHTML()
    parser.feed(text)
    return "".join(parser.parts), parser.has_media


def aliases(person):
    values = set()
    for field in ("DisplayName", "FirstName", "LastName", "Username", "Email", "OrgDefinedId"):
        value = normalize(str(person.get(field) or "")).strip()
        if len(value) >= 2:
            values.add(value)
    first, last = person.get("FirstName"), person.get("LastName")
    if first and last:
        values.update([normalize(f"{first} {last}"), normalize(f"{last}, {first}")])
    for field in ("FirstName", "LastName", "DisplayName"):
        for token in re.findall(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", person.get(field) or ""):
            if len(token) >= 3:
                values.add(normalize(token))
    return {re.sub(r"\s+", " ", value) for value in values}


class Redactor:
    def __init__(self, people, owner, key):
        self.key = key
        terms = {}
        for uid, person in people.items():
            for value in aliases(person):
                terms.setdefault(value.casefold(), set()).add(str(uid))
        self.terms = terms
        self.owner = str(owner)
        # Escaped literals; flexible whitespace accommodates line-wrapped names.
        patterns = [r"\s+".join(re.escape(p) for p in value.split())
                    for value in sorted(terms, key=len, reverse=True)]
        self.pattern = re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.I) if patterns else None

    def clean(self, text):
        text = normalize(text)
        count = 0
        if self.pattern:
            def replace(match):
                term = re.sub(r"\s+", " ", match.group()).casefold()
                owners = self.terms.get(term, set())
                return self.key if owners == {self.owner} else "[PERSON REDACTED]"
            text, count = self.pattern.subn(replace, text)
        for pattern, replacement in [
            (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL REDACTED]"),
            (r"(?:https?://|www\.)[^\s<>]+", "[LINK REMOVED]"),
            (r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b", "[PHONE REDACTED]"),
            (r"\b(?:student\s*id|vunetid|username)\s*[:=]\s*\S+", "[IDENTIFIER REDACTED]"),
        ]:
            text, n = re.subn(pattern, replacement, text, flags=re.I)
            count += n
        # Known-identifier check excludes the generated key itself.
        check = text.replace(self.key, "")
        require(not self.pattern or not self.pattern.search(check), "RESIDUAL_IDENTIFIER")
        return text, count

    def check_result(self, value):
        text = normalize(json.dumps(value, ensure_ascii=False)).replace(self.key, "")
        require(not self.pattern or not self.pattern.search(text), "RESULT_NEEDS_REDACTION_REVIEW")
        require(not re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I),
                "RESULT_NEEDS_REDACTION_REVIEW")


def extract(data, suffix):
    """Return text only. Original files/media/properties never leave private storage."""
    require(len(data) <= MAX_BYTES, "FILE_TOO_LARGE")
    suffix = suffix.lower()
    if suffix in (".txt", ".md", ".csv", ".py", ".json"):
        text = data.decode("utf-8-sig", errors="strict")
        require("\x00" not in text, "UNREADABLE_TEXT")
        require(not re.search(r"!\[|<img\b|data:image/", text, re.I), "VISUAL_REVIEW_REQUIRED")
        return text
    if suffix in (".html", ".htm"):
        text, media = plain_html(data.decode("utf-8-sig", errors="strict"))
        require(not media, "VISUAL_REVIEW_REQUIRED")
        return text
    if suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require(sum(i.file_size for i in archive.infolist()) <= MAX_BYTES, "FILE_TOO_LARGE")
            names = archive.namelist()
            require(not any(n.startswith(("word/media/", "word/embeddings/")) for n in names),
                    "VISUAL_REVIEW_REQUIRED")
            xml = archive.read("word/document.xml")
            require(b"<!DOCTYPE" not in xml and b"<!ENTITY" not in xml, "UNSUPPORTED_XML")
            root = ET.fromstring(xml)
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            require(not any(n.tag in {ns + "del", ns + "ins", ns + "drawing", ns + "pict", ns + "object"}
                            for n in root.iter()), "DOCUMENT_REVIEW_REQUIRED")
            return "\n".join("".join(t.text or "" for t in p.iter(ns + "t"))
                             for p in root.iter(ns + "p"))
    if suffix == ".pdf":
        import fitz
        with fitz.open(stream=data, filetype="pdf") as document:
            require(not document.needs_pass and len(document) > 0, "UNREADABLE_PDF")
            text = []
            for page in document:
                require(not page.get_images() and not page.get_drawings(), "VISUAL_REVIEW_REQUIRED")
                content = page.get_text(sort=True)
                require(content.strip(), "OCR_REVIEW_REQUIRED")
                text.append(content)
            return "\n\n".join(text)
    raise Stop("UNSUPPORTED_FORMAT")


class Brightspace:
    """Existing client-owned auth; controlled diagnostics and bounded same-host requests."""
    def __init__(self, host):
        import requests
        self.host = host
        self.session = requests.Session()
        client = Path.home() / ".codex/skills/brightspace-course/scripts/bsapi.py"
        require(client.is_file(), "AUTH_CLIENT_MISSING")
        os.environ["BRIGHTSPACE_HOST"] = host
        spec = importlib.util.spec_from_file_location("_lab_bsapi", client)
        module = importlib.util.module_from_spec(spec)
        # This client can print diagnostics; suppress them at the private boundary.
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            try:
                spec.loader.exec_module(module)
                token = module.get_token()
            except (Exception, SystemExit):
                raise Stop("AUTH_UNAVAILABLE") from None
        self.session.headers["Authorization"] = "Bearer " + token

    def request(self, method, path, payload=None, missing=False, binary=False, allow_html=False):
        url = urlsplit(urljoin("https://" + self.host, path))
        require(url.scheme == "https" and url.netloc == self.host and url.path.startswith("/d2l/api/"),
                "API_DESTINATION_REFUSED")
        response = self.session.request(method, url.geturl(), json=payload,
                                        timeout=60, allow_redirects=False, stream=True)
        with response:
            if response.status_code == 404 and missing:
                return None
            require(response.status_code in (200, 201, 204),
                    "AUTH_UNAVAILABLE" if response.status_code == 401 else "API_REQUEST_FAILED")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                require(size <= MAX_BYTES, "RESPONSE_TOO_LARGE")
                chunks.append(chunk)
            data = b"".join(chunks)
            if binary:
                require(allow_html or "text/html" not in response.headers.get("Content-Type", "").lower(),
                        "DOWNLOAD_RETURNED_HTML")
                return data
            return json.loads(data) if data else None

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload):
        return self.request("POST", path, payload)

    def collection(self, path):
        seen, result = set(), []
        for _ in range(1000):
            require(path not in seen, "PAGINATION_LOOP")
            seen.add(path)
            page = self.get(path)
            if isinstance(page, list):
                return result + page
            require(isinstance(page, dict), "INVALID_COLLECTION")
            items = page.get("Objects", page.get("Items"))
            require(isinstance(items, list), "INVALID_COLLECTION")
            result.extend(items)
            nxt = page.get("Next")
            info = page.get("PagingInfo") or {}
            if not nxt and info.get("HasMoreItems"):
                require(info.get("Bookmark"), "INVALID_PAGINATION")
                url = urlsplit(path)
                query = dict(parse_qsl(url.query))
                query["bookmark"] = info["Bookmark"]
                nxt = url.path + "?" + urlencode(query)
            if not nxt:
                return result
            path = urljoin("https://" + self.host + "/", nxt)
        raise Stop("PAGINATION_LIMIT")


def select_submissions(entity, policy):
    submissions = entity.get("Submissions")
    require(isinstance(submissions, list), "INVALID_SUBMISSIONS")
    require(len({identifier(s["Id"]) for s in submissions}) == len(submissions), "DUPLICATE_SUBMISSION")
    if not submissions:
        return []
    require(all(s.get("SubmissionDate") for s in submissions), "MISSING_SUBMISSION_DATE")
    ordered = sorted(submissions, key=lambda s: (s["SubmissionDate"], int(s["Id"])))
    return ordered if policy == "all" else ordered[-1:]


def revision(entity, policy):
    # Exclude read/flag state and feedback, which can change without new student work.
    return digest([{ "Id": s["Id"], "SubmissionDate": s["SubmissionDate"],
        "Comment": s.get("Comment"), "Files": [{k: f.get(k) for k in ("FileId", "FileName", "Size")}
                                                for f in s.get("Files", [])]}
        for s in select_submissions(entity, policy)])


class Pipeline:
    def __init__(self, root, lab):
        require(SLUG_RE.fullmatch(lab), "INVALID_LAB")
        self.root = Path(root)
        self.lab = lab
        self.labroot = self.root / "labs" / lab
        require((self.labroot / "grading-config.json").is_file(), "GRADING_CONFIG_REQUIRED")
        self.config = read_json(self.labroot / "grading-config.json")
        self.validate_config()
        self.private = self.root / "FERPA-sensitive" / "lab-grading"
        self.state = self.private / "labs" / lab
        self.mapping = self.private / "courses" / self.config["course_scope"] / "identities.json"
        self.released = self.labroot / "submissions" / "anonymous"
        self.results = self.labroot / "grading" / "results"
        for path in (self.private, self.state, self.mapping, self.released, self.results):
            no_links(path)

    def validate_config(self):
        cfg = self.config
        require(cfg.get("schema") == 1 and SLUG_RE.fullmatch(cfg.get("course_scope", "")), "INVALID_CONFIG")
        require(re.fullmatch(r"[a-z0-9.-]+", cfg.get("host", "")) and "." in cfg["host"], "INVALID_HOST")
        require(re.fullmatch(r"1\.\d+", cfg.get("le_version", "")), "INVALID_VERSION")
        require(cfg.get("attempt_policy") in ("latest", "all"), "ATTEMPT_POLICY_REQUIRED")
        require(isinstance(cfg.get("sections"), list) and cfg["sections"], "SECTIONS_REQUIRED")
        pairs = []
        for section in cfg["sections"]:
            pairs.append((identifier(section["org_unit_id"]), identifier(section["folder_id"])))
            require(section.get("course_name") and section.get("assignment_name"), "EXPECTED_NAMES_REQUIRED")
        require(len(set(pairs)) == len(pairs), "DUPLICATE_SECTION")
        criteria = cfg.get("criteria", [])
        require(criteria and len({c["id"] for c in criteria}) == len(criteria), "INVALID_RUBRIC")
        for criterion in criteria:
            require(SLUG_RE.fullmatch(criterion["id"]) and criterion.get("label") and
                    number(criterion["possible"]) > 0, "INVALID_RUBRIC")
        self.possible = sum(number(c["possible"]) for c in criteria)

    def base(self, section):
        return f'/d2l/api/le/{self.config["le_version"]}/{identifier(section["org_unit_id"])}/dropbox/folders/{identifier(section["folder_id"])}'

    def identities(self):
        value = read_json(self.mapping) if self.mapping.exists() else {
            "host": self.config["host"], "course_scope": self.config["course_scope"], "people": {}}
        require(value["host"] == self.config["host"], "COURSE_SCOPE_HOST_CONFLICT")
        keys = [p["key"] for p in value["people"].values()]
        require(len(keys) == len(set(keys)) and all(KEY_RE.fullmatch(k) for k in keys), "INVALID_IDENTITY_MAP")
        return value

    def check_folder(self, api, section):
        ou = identifier(section["org_unit_id"])
        course = api.get(f"/d2l/api/lp/1.57/courses/{ou}")
        require(course.get("Name") == section["course_name"], "COURSE_NAME_CHANGED")
        folder = api.get(self.base(section))
        require(str(folder.get("Id")) == str(section["folder_id"]) and
                folder.get("Name") == section["assignment_name"], "ASSIGNMENT_CHANGED")
        require(not folder.get("GroupTypeId") and folder.get("DropboxType") in (2, "Individual"),
                "GROUP_WORK_UNSUPPORTED")
        require(math.isclose(number((folder.get("Assessment") or {}).get("ScoreDenominator")), self.possible),
                "RUBRIC_TOTAL_MISMATCH")
        return folder

    def download(self, api):
        identity = self.identities()
        # Revoke the current grading release before refreshing it. Old snapshots remain private.
        self.revoke()
        save(self.state / "snapshot.json", {"status": "downloading"})
        run = uuid.uuid4().hex
        runroot = self.state / "raw" / run
        records, seen = [], set()
        for section_index, section in enumerate(self.config["sections"]):
            folder = self.check_folder(api, section)
            ou = identifier(section["org_unit_id"])
            people = api.collection(f'/d2l/api/le/{self.config["le_version"]}/{ou}/classlist/paged/')
            for person in people:
                uid = identifier(person["Identifier"])
                stored = identity["people"].setdefault(uid, {"key": "S-" + uuid.uuid4().hex, "aliases": []})
                stored["aliases"] = sorted(set(stored["aliases"]) | aliases(person))
            entities = api.collection(self.base(section) + "/submissions/paged/")
            save(runroot / f"section-{section_index}-envelopes.json", entities)
            for entity in entities:
                require(entity["Entity"]["EntityType"] == "User", "GROUP_WORK_UNSUPPORTED")
                uid = identifier(entity["Entity"]["EntityId"])
                if not entity.get("Submissions"):
                    continue
                require(uid in identity["people"] and identity["people"][uid]["aliases"], "IDENTITY_LOOKUP_INCOMPLETE")
                require(uid not in seen, "DUPLICATE_STUDENT_ACROSS_SECTIONS")
                seen.add(uid)
                person = identity["people"][uid]
                extra = aliases({"DisplayName": entity["Entity"].get("DisplayName")})
                person["aliases"] = sorted(set(person["aliases"]) | extra)
                key = person["key"]
                files = {}
                # Download all attempts; attempt selection happens during preparation.
                for sub in entity["Submissions"]:
                    sid = identifier(sub["Id"])
                    for file in sub.get("Files", []):
                        fid = identifier(file["FileId"])
                        require((sid, fid) not in files, "DUPLICATE_FILE")
                        options = {"binary": True}
                        if Path(file["FileName"]).suffix.lower() in (".html", ".htm"):
                            options["allow_html"] = True
                        data = api.get(self.base(section) + f"/submissions/{sid}/files/{fid}", **options)
                        require(len(data) == file["Size"], "FILE_SIZE_MISMATCH")
                        path = runroot / key / f"{sid}-{fid}.bin"
                        save(path, data)
                        files[(sid, fid)] = {"relative": str(path.relative_to(self.state)), "sha256": digest(data)}
                record = {"key": key, "user_id": uid, "section": section, "folder": folder,
                          "entity": entity, "revision": revision(entity, self.config["attempt_policy"]),
                          "files": {f"{sid}/{fid}": value for (sid, fid), value in files.items()}}
                save(runroot / key / "record.json", record)
                records.append(str((runroot / key / "record.json").relative_to(self.state)))
        save(self.mapping, identity)
        save(self.state / "snapshot.json", {"status": "complete", "run": run,
                                            "config_digest": digest(self.config), "records": records})
        return {"downloaded": len(records)}

    def revoke(self):
        if self.released.exists():
            no_links(self.released)
            # Keep old sanitized releases privately for audit; no stale agent-visible package.
            archive = self.state / "old-releases" / uuid.uuid4().hex
            archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.released.rename(archive)

    def records(self):
        require((self.state / "snapshot.json").is_file(), "DOWNLOAD_REQUIRED")
        snapshot = read_json(self.state / "snapshot.json")
        require(snapshot.get("status") == "complete", "DOWNLOAD_INCOMPLETE")
        require(snapshot["config_digest"] == digest(self.config), "CONFIG_CHANGED_DOWNLOAD_AGAIN")
        records = []
        for relative in snapshot["records"]:
            path = self.state / relative
            require(path.resolve().is_relative_to(self.state.resolve()), "PRIVATE_PATH_REFUSED")
            no_links(path)
            records.append(read_json(path))
        return records

    def redactor(self, record):
        identity = self.identities()
        # Feed each alias as a DisplayName while keeping its owner binding.
        redactor = Redactor({}, record["user_id"], record["key"])
        terms = {}
        for uid, person in identity["people"].items():
            for term in person["aliases"]:
                terms.setdefault(normalize(term).casefold(), set()).add(uid)
        redactor.terms = terms
        patterns = [r"\s+".join(re.escape(p) for p in term.split())
                    for term in sorted(terms, key=len, reverse=True)]
        redactor.pattern = re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.I) if patterns else None
        return redactor

    def prepare(self):
        records = self.records()
        self.revoke()
        released, held = 0, 0
        for record in records:
            key, pieces, reasons = record["key"], [], []
            for index, sub in enumerate(select_submissions(record["entity"], self.config["attempt_policy"]), 1):
                comment = sub.get("Comment") or {}
                if comment.get("Html"):
                    text, media = plain_html(comment["Html"])
                    if media:
                        reasons.append("VISUAL_REVIEW_REQUIRED")
                else:
                    text = comment.get("Text") or ""
                if text.strip():
                    pieces.append(f"## Attempt {index}: submission text\n\n{text}")
                for file_index, file in enumerate(sub.get("Files", []), 1):
                    entry = record["files"][f'{sub["Id"]}/{file["FileId"]}']
                    path = self.state / entry["relative"]
                    require(path.resolve().is_relative_to(self.state.resolve()), "PRIVATE_PATH_REFUSED")
                    no_links(path)
                    data = path.read_bytes()
                    require(digest(data) == entry["sha256"], "RAW_FILE_CHANGED")
                    try:
                        text = extract(data, Path(file["FileName"]).suffix)
                        require(text.strip(), "EMPTY_EXTRACTION")
                        pieces.append(f"## Attempt {index}: file {file_index}\n\n{text}")
                    except Exception as exc:
                        reasons.append(str(exc) if isinstance(exc, Stop) else "EXTRACTION_FAILED")
            if not pieces:
                reasons.append("NO_GRADABLE_TEXT")
            redactor = self.redactor(record)
            text, count = redactor.clean("\n\n".join(pieces))
            review = self.state / "review" / key
            save(review / "submission.txt", text.encode())
            save(review / "status.json", {"revision": record["revision"], "reasons": sorted(set(reasons)),
                                          "replacements": count, "text_digest": digest(text.encode())})
            if reasons:
                held += 1
            else:
                self.release(record, text, "automatic")
                released += 1
        return {"released": released, "held": held}

    def release(self, record, text, mode):
        key = record["key"]
        require(KEY_RE.fullmatch(key) and text.strip(), "INVALID_RELEASE")
        text, _ = self.redactor(record).clean(text)
        packet = {"schema": 1, "student_key": key, "assignment": self.lab,
                  "revision_token": uuid.uuid4().hex, "criteria": self.config["criteria"],
                  "possible": self.possible, "submission": text,
                  "preprocessing": "Identifiers and links removed locally; do not penalize redaction placeholders."}
        packet["package_digest"] = digest(packet)
        previous_path = self.state / "releases" / (key + ".json")
        if previous_path.exists():
            previous = read_json(previous_path)
            if (previous["revision"] == record["revision"] and previous["packet"]["submission"] == text
                    and previous["packet"]["criteria"] == self.config["criteria"]):
                packet = previous["packet"]
        private_release = {"revision": record["revision"], "packet": packet, "mode": mode}
        save(self.state / "releases" / (key + ".json"), private_release)
        save(self.released / key / "packet.json", packet)
        save(self.released / key / "submission.txt", text.encode())

    def release_reviewed(self, key):
        require(KEY_RE.fullmatch(key), "INVALID_KEY")
        record = next((r for r in self.records() if r["key"] == key), None)
        require(record is not None, "UNKNOWN_KEY")
        status = read_json(self.state / "review" / key / "status.json")
        require(status["revision"] == record["revision"], "STALE_REVIEW")
        text = (self.state / "review" / key / "submission.txt").read_text()
        self.release(record, text, "human-reviewed")
        return {"released": 1}

    def validate_result(self, record):
        key = record["key"]
        release = read_json(self.state / "releases" / (key + ".json"))
        require(release["revision"] == record["revision"], "STALE_RELEASE")
        packet = read_json(self.released / key / "packet.json")
        require(packet == release["packet"], "RELEASE_CHANGED")
        require((self.released / key / "submission.txt").read_text() == packet["submission"], "RELEASE_CHANGED")
        result_path = self.results / key / "result.json"
        no_links(result_path)
        result = read_json(result_path)
        require(set(result) == {"student_key", "package_digest", "status", "criteria", "score", "feedback"},
                "RESULT_SCHEMA_MISMATCH")
        require(result.get("student_key") == key and result.get("package_digest") == packet["package_digest"],
                "RESULT_IDENTITY_MISMATCH")
        require(result.get("status") == "complete", "PROVISIONAL_RESULT")
        criteria = result.get("criteria", [])
        require(len(criteria) == len(self.config["criteria"]) and
                {c["id"] for c in criteria} == {c["id"] for c in self.config["criteria"]}, "RESULT_RUBRIC_MISMATCH")
        total = 0
        rows = []
        for spec in self.config["criteria"]:
            criterion = next(c for c in criteria if c["id"] == spec["id"])
            require(set(criterion) == {"id", "earned", "evidence", "deduction_reason"}, "RESULT_SCHEMA_MISMATCH")
            earned, possible = number(criterion["earned"]), number(spec["possible"])
            require(0 <= earned <= possible, "INVALID_POINTS")
            require(isinstance(criterion.get("evidence"), str) and criterion["evidence"].strip(), "MISSING_EVIDENCE")
            if earned < possible:
                require(isinstance(criterion.get("deduction_reason"), str) and criterion["deduction_reason"].strip(),
                        "MISSING_DEDUCTION_REASON")
            total += earned
            rows.append((spec, criterion))
        require(math.isclose(number(result.get("score")), total, abs_tol=1e-8), "SCORE_SUM_MISMATCH")
        require(isinstance(result.get("feedback"), str) and result["feedback"].strip(), "MISSING_FEEDBACK")
        require(len(result["feedback"]) < 100000, "FEEDBACK_TOO_LONG")
        # Known identities stay out; course-note URLs may remain in feedback as escaped text.
        self.redactor(record).check_result(result)
        sections = [f"Score: {total:g} / {self.possible:g}", "", "Rubric breakdown"]
        for spec, criterion in rows:
            parts = [f'{spec["label"]}: {criterion["earned"]:g} / {spec["possible"]:g}',
                     "Evidence: " + criterion["evidence"]]
            if criterion["earned"] < spec["possible"]:
                parts.append(f'Points deducted: {spec["possible"] - criterion["earned"]:g}. ' + criterion["deduction_reason"])
            sections.extend(["", *parts])
        sections.extend(["", result["feedback"]])
        text = "\n".join(sections)
        self.redactor(record).check_result(text)
        # HTML is generated only from escaped text, never trusted agent HTML.
        body = "<div>" + "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>"
                                    for p in text.split("\n\n")) + "</div>"
        save(self.results / key / "feedback.txt", text.encode())
        save(self.results / key / "feedback.html", body.encode())
        return result, {"Score": total, "Feedback": {"Text": text, "Html": body},
                        "RubricAssessments": [], "IsGraded": False, "GradedSymbol": None}

    def validate(self):
        records = {r["key"]: r for r in self.records()}
        paths = list(self.results.glob("*/result.json"))
        require(paths, "NO_RESULTS")
        for path in paths:
            require(path.parent.name in records, "UNKNOWN_RESULT_KEY")
            self.validate_result(records[path.parent.name])
        return {"validated": len(paths)}

    def status(self):
        records = self.records()
        released = sum((self.released / r["key"] / "packet.json").exists() for r in records)
        results = sum((self.results / r["key"] / "result.json").exists() for r in records)
        return {"downloaded": len(records), "released": released, "held": len(records) - released,
                "result_files": results}

    def feedback_path(self, record):
        return self.base(record["section"]) + "/feedback/user/" + identifier(record["user_id"])

    def grade_path(self, record, grade_item):
        ou = identifier(record["section"]["org_unit_id"])
        return f'/d2l/api/le/{self.config["le_version"]}/{ou}/grades/{identifier(grade_item)}/values/{record["user_id"]}'

    def live_revision(self, api, record):
        entity = api.get(self.base(record["section"]) + "/submissions/user/" + identifier(record["user_id"]))
        require(str(entity["Entity"]["EntityId"]) == record["user_id"], "API_IDENTITY_MISMATCH")
        require(revision(entity, self.config["attempt_policy"]) == record["revision"], "NEW_SUBMISSION_DOWNLOAD_AGAIN")

    def plan_upload(self, api, publish=False):
        self.validate()
        require(all((self.results / r["key"] / "result.json").exists() for r in self.records()
                    if (self.released / r["key"] / "packet.json").exists()), "MISSING_RELEASED_RESULTS")
        entries = []
        for record in self.records():
            key = record["key"]
            if not (self.results / key / "result.json").exists():
                continue
            folder = self.check_folder(api, record["section"])
            require(folder.get("GradeItemId") is not None, "GRADE_LINK_REQUIRED")
            self.live_revision(api, record)
            result, payload = self.validate_result(record)
            payload["IsGraded"] = publish
            before = api.get(self.feedback_path(record), missing=True)
            receipt_path = self.state / "receipts" / (key + ".json")
            receipt = read_json(receipt_path) if receipt_path.exists() else None
            require(not receipt or receipt.get("state") != "pending", "UNCERTAIN_WRITE_REVIEW_REQUIRED")
            if before and not empty_feedback(before):
                require(receipt and receipt.get("state") == "verified" and
                        feedback_matches(before, receipt["payload"]), "EXISTING_FEEDBACK_CONFLICT")
                # An owned draft may be published. Published feedback is never silently revised.
                require(not before.get("IsGraded") or feedback_matches(before, payload), "PUBLISHED_FEEDBACK_CONFLICT")
                payload["RubricAssessments"] = before.get("RubricAssessments") or []
            grade_before = api.get(self.grade_path(record, folder["GradeItemId"]), missing=True)
            if grade_before and grade_before.get("PointsNumerator") is not None:
                require(receipt and receipt.get("state") == "verified" and receipt["payload"]["IsGraded"] and
                        number(grade_before["PointsNumerator"]) == receipt["payload"]["Score"],
                        "EXISTING_GRADE_CONFLICT")
            entries.append({"key": key, "result_digest": digest(result), "payload": payload,
                            "before": before, "grade_before": grade_before,
                            "grade_item_id": identifier(folder["GradeItemId"])})
        save(self.state / "upload-plan.json", {"config_digest": digest(self.config), "entries": entries,
                                               "publish": publish})
        return {"planned": len(entries), "publish": int(publish)}

    def upload(self, api):
        plan = read_json(self.state / "upload-plan.json")
        require(plan["config_digest"] == digest(self.config), "STALE_PLAN")
        proof = self.private / "verified-feedback-hosts.json"
        verified = read_json(proof) if proof.exists() else {}
        require(self.config.get("test_course") is True or self.config["host"] in verified,
                "LIVE_FEEDBACK_TEST_REQUIRED")
        records = {r["key"]: r for r in self.records()}
        completed = 0
        for entry in plan["entries"]:
            key, payload = entry["key"], entry["payload"]
            require(key in records, "STALE_PLAN")
            record = records[key]
            result, expected = self.validate_result(record)
            require(digest(result) == entry["result_digest"], "RESULT_CHANGED_REPLAN")
            expected["IsGraded"] = plan["publish"]
            expected["RubricAssessments"] = payload["RubricAssessments"]
            require(expected == payload, "PAYLOAD_CHANGED_REPLAN")
            folder = self.check_folder(api, record["section"])
            require(str(folder["GradeItemId"]) == entry["grade_item_id"], "GRADE_LINK_CHANGED")
            self.live_revision(api, record)
            path = self.feedback_path(record)
            receipt_path = self.state / "receipts" / (key + ".json")
            receipt = read_json(receipt_path) if receipt_path.exists() else None
            current = api.get(path, missing=True)
            grade_current = api.get(self.grade_path(record, entry["grade_item_id"]), missing=True)
            if not feedback_matches(current, payload):
                require(not receipt or receipt.get("state") != "pending", "UNCERTAIN_WRITE_REVIEW_REQUIRED")
                require(current == entry["before"], "FEEDBACK_CHANGED_REPLAN")
                require(grade_current == entry["grade_before"], "GRADE_CHANGED_REPLAN")
                save(receipt_path, {"state": "pending", "payload": payload, "before": current})
                api.post(path, payload)
            saved = api.get(path)
            require(feedback_matches(saved, payload), "FEEDBACK_READBACK_FAILED")
            before = entry["before"] or {}
            require(saved.get("Files", []) == before.get("Files", []) and
                    saved.get("Links", []) == before.get("Links", []), "FEEDBACK_ATTACHMENTS_CHANGED")
            if plan["publish"]:
                grade = api.get(self.grade_path(record, entry["grade_item_id"]))
                require(math.isclose(number(grade.get("PointsNumerator")), payload["Score"]), "GRADE_READBACK_FAILED")
            save(receipt_path, {"state": "verified", "payload": payload, "saved": saved})
            completed += 1
        # A test-course publication verifies both feedback persistence and grade linkage.
        if completed and self.config.get("test_course") is True and plan["publish"]:
            verified[self.config["host"]] = {"test_lab": self.lab, "verified": True}
            save(proof, verified)
        return {"verified_uploads": completed}

    def export_review(self):
        self.validate()
        out = self.labroot / "grading" / "ta-review.zip"
        temp = io.BytesIO()
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for record in self.records():
                key = record["key"]
                if not (self.results / key / "result.json").exists():
                    continue
                result, _ = self.validate_result(record)
                packet = read_json(self.released / key / "packet.json")
                archive.writestr(key + "/submission.txt", packet["submission"])
                archive.writestr(key + "/result.json", json.dumps(result, indent=2))
                archive.writestr(key + "/feedback.html", (self.results / key / "feedback.html").read_bytes())
        save(out, temp.getvalue())
        return {"review_archives": 1}


def empty_feedback(value):
    return not (value.get("Score") is not None or value.get("IsGraded") or
                any((value.get("Feedback") or {}).values()) or value.get("RubricAssessments") or
                value.get("Files") or value.get("Links"))


def feedback_matches(current, payload):
    if not current:
        return False
    # Exact plain text and HTML read-back; an unexpected server transformation stops verification.
    return all(current.get(k) == payload.get(k) for k in
               ("Score", "Feedback", "IsGraded", "RubricAssessments", "GradedSymbol"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check-config", "status", "download", "prepare", "release-reviewed",
                                           "validate", "export-review", "plan-upload", "upload"])
    parser.add_argument("--lab", required=True)
    parser.add_argument("--key")
    parser.add_argument("--human-reviewed", action="store_true")
    parser.add_argument("--publish", action="store_true", help="Only for plan-upload; otherwise draft")
    parser.add_argument("--execute", action="store_true", help="Required for upload; applies saved plan")
    args = parser.parse_args(argv)
    os.umask(0o077)
    require(SLUG_RE.fullmatch(args.lab), "INVALID_LAB")
    require(Path(__file__).resolve().parent.parent.name == "ferpa-safe", "INSTALL_IN_COURSE_REQUIRED")
    pipe = Pipeline(ROOT, args.lab)
    with lock(pipe.private / ".pipeline.lock"):
        if args.command == "check-config":
            return {"sections": len(pipe.config["sections"]), "criteria": len(pipe.config["criteria"])}
        if args.command == "prepare":
            return pipe.prepare()
        if args.command == "status":
            return pipe.status()
        if args.command == "release-reviewed":
            require(args.human_reviewed, "HUMAN_REVIEW_REQUIRED")
            return pipe.release_reviewed(args.key or "")
        if args.command == "validate":
            return pipe.validate()
        if args.command == "export-review":
            return pipe.export_review()
        if args.command == "upload":
            require(args.execute, "EXECUTE_REQUIRED")
        api = Brightspace(pipe.config["host"])
        if args.command == "download":
            return pipe.download(api)
        if args.command == "plan-upload":
            return pipe.plan_upload(api, args.publish)
        return pipe.upload(api)


def safe_main(argv=None):
    # Also redirect OS descriptors: PDF parsers may write native-library diagnostics.
    saved_fds = [os.dup(1), os.dup(2)]
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            result = main(argv)
            output, code = {"status": "ok", **result}, 0
        except Stop as exc:
            output, code = {"status": "stopped", "code": str(exc)}, 1
        except (Exception, SystemExit, KeyboardInterrupt):
            output, code = {"status": "stopped", "code": "LOCAL_OPERATION_FAILED"}, 1
        finally:
            for descriptor, previous in zip((1, 2), saved_fds):
                os.dup2(previous, descriptor)
                os.close(previous)
    print(json.dumps(output))
    return code


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        main(["--help"])
    else:
        sys.exit(safe_main())
