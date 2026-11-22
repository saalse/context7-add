#!/usr/bin/env python3
"""Context7 add + allowlist helper (stdlib only).
Part of the context7-add skill by Alex Samson (https://saalse.com), MIT license.

Every command that changes something is a dry run unless --apply is passed.

  c7.py whoami                         plan, teamspace, role, policy mode, allowlist size
  c7.py check <url|/org/repo|name>...  is it in Context7? is it allowed by the policy?
  c7.py allow <entry>... [--remove] [--apply]
                                       add/remove allowlist entries (/org, /org/repo, domain.com)
  c7.py add <url>... [--type T] [--private] [--generate-docs] [--base-url URL]
              [--git-token-env VAR] [--queue [--queue-timeout SEC]] [--apply]
                                       submit repos / websites / llms.txt / OpenAPI URLs;
                                       --queue: wait for a free slot when Context7's
                                       5-submission queue is full, instead of stopping.
                                       One progress line per URL on stderr; --type and
                                       --base-url need a single URL
  c7.py upload <file> [--title T] [--description D] [--skip-size-check] [--apply]
                                       upload a PDF, DOCX or OpenAPI file (max ~4.5 MB)
  c7.py refresh <libraryId> [--apply]  re-crawl / re-parse a stale library (daily limit)
  c7.py wait <libraryId> [--changed] [--timeout SEC]
                                       poll until the library is indexed and served;
                                       --changed: until a refresh lands (lastUpdateDate moves)
  c7.py log                            show what this skill has changed before

Examples:
  c7.py check https://github.com/saalse/context7-add https://docs.acme.dev/
  c7.py allow /saalse/context7-add --apply
  c7.py add https://docs.acme.dev/guide/ --base-url https://docs.acme.dev/guide/ --apply
  c7.py add https://github.com/acme/a https://github.com/acme/b https://docs.acme.dev/ --queue --apply

API key: $CONTEXT7_API_KEY, else the context7 MCP entry in ~/.claude.json.
The key is never printed. Results are JSON on stdout; progress and errors on stderr.

Exit codes:
  0  success (check: the report was produced, read its fields; wait: ready)
  1  API or network error
  2  bad input (arguments, file type, unsupported llms.txt name, file too large / 413)
  3  auth: missing/invalid API key, or role/plan too low
  4  library not found
  5  refused by the teamspace policy (access_denied)
  6  rate limited (429)
  7  library unprocessable (no or too few docs, or not a documentation website)
  8  wait timed out
  9  submission queue full (5 libraries already processing). add: resubmit later (with
     several URLs, `notSubmitted` lists which) or rerun with --queue; refresh / upload:
     try again later
  130 add: interrupted (Ctrl-C or SIGTERM); the partial summary lists `notSubmitted`
With several URLs, add exits 130 if interrupted, else with the first failure (a refusal or a
network error, listed in `failed`), else 3, 9 or 6. A 401, 429 or full queue stops the run; that URL and the rest are
listed in `notSubmitted`.
"""
import argparse
import http.client
import json
import mimetypes
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

API = "https://context7.com/api"
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(SKILL_DIR, "registry.jsonl")

EXIT_API, EXIT_USAGE, EXIT_AUTH, EXIT_NOT_FOUND = 1, 2, 3, 4
EXIT_POLICY, EXIT_RATE, EXIT_UNPROCESSABLE, EXIT_TIMEOUT, EXIT_QUEUE = 5, 6, 7, 8, 9
EXIT_INTERRUPTED = 130

QUEUE_FULL = "user-has-active-task"  # 400: "You already have 5 libraries being processed or queued"
QUEUE_POLL_SEC = 30
DEFAULT_QUEUE_TIMEOUT = 1800
# Context7 doesn't document an upload limit; in practice the request body is capped at ~4.5 MB
# (413 FUNCTION_PAYLOAD_TOO_LARGE: ~3 MB goes through, ~5 MB does not). Leave room for the
# multipart envelope.
MAX_UPLOAD_BYTES = 4_400_000
LARGE_UPLOAD_BYTES = 3_000_000  # above this, a dropped connection may be the size limit


def fail(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def http_exit_code(status, data):
    err = data.get("error") if isinstance(data, dict) else None
    if status == 403:
        return EXIT_POLICY if err == "access_denied" else EXIT_AUTH
    if err == QUEUE_FULL:
        return EXIT_QUEUE
    return {0: EXIT_API, 400: EXIT_USAGE, 401: EXIT_AUTH, 404: EXIT_NOT_FOUND, 413: EXIT_USAGE,
            422: EXIT_UNPROCESSABLE, 429: EXIT_RATE}.get(status, EXIT_API)


def queue_full(data):
    return isinstance(data, dict) and data.get("error") == QUEUE_FULL


REPO_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "bitbucket.org": "bitbucket"}
ADD_ENDPOINT = {
    "github": ("/v2/add/repo/github", "docsRepoUrl"),
    "gitlab": ("/v2/add/repo/gitlab", "docsRepoUrl"),
    "bitbucket": ("/v2/add/repo/bitbucket", "docsRepoUrl"),
    "git": ("/v2/add/repo/git", "docsRepoUrl"),
    "website": ("/v2/add/website", "websiteUrl"),
    "llmstxt": ("/v2/add/llmstxt", "llmstxtUrl"),
    "openapi": ("/v2/add/openapi", "openApiUrl"),
}
UPLOAD_ENDPOINT = {
    ".pdf": ("/v2/add/pdf-upload", "pdfFile", "projectTitle"),
    ".docx": ("/v2/add/docx-upload", "docxFile", "projectTitle"),
    ".json": ("/v2/add/openapi-upload", "openapiFile", "libraryTitle"),
    ".yaml": ("/v2/add/openapi-upload", "openapiFile", "libraryTitle"),
    ".yml": ("/v2/add/openapi-upload", "openapiFile", "libraryTitle"),
}


# ---------- plumbing ----------

def api_key():
    key = os.environ.get("CONTEXT7_API_KEY")
    if key:
        return key
    try:
        with open(os.path.expanduser("~/.claude.json")) as f:
            servers = json.load(f).get("mcpServers", {})
    except (OSError, ValueError):
        servers = {}
    for name, cfg in servers.items():
        if "context7" not in name.lower() or "admin" in name.lower():
            continue
        headers = cfg.get("headers", {}) or {}
        if headers.get("CONTEXT7_API_KEY"):
            return headers["CONTEXT7_API_KEY"]
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
    fail(EXIT_AUTH, "No Context7 API key: set CONTEXT7_API_KEY or configure the context7 MCP server.")


def call(method, path, body=None, params=None, form=None):
    """Return (status, parsed_json_or_text)."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Authorization": "Bearer " + api_key(), "User-Agent": "context7-add-skill"}
    data = None
    if form is not None:
        data, ctype = form
        headers["Content-Type"] = ctype
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:  # the server may send error headers and then drop the connection (seen with 413)
            raw = e.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        finally:
            try:
                e.close()
            except (OSError, http.client.HTTPException):
                pass
    except urllib.error.URLError as e:
        return 0, {"error": "network", "message": str(e.reason)}
    except (OSError, http.client.HTTPException) as e:
        # Connection reset, remote disconnect or timeout mid-request (seen on large uploads):
        # report it as JSON like any other network error instead of a traceback.
        return 0, {"error": "network", "message": f"{type(e).__name__}: {e}"}
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def log(action, **fields):
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, **fields}
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:  # the change itself went through; don't lose the result over the audit log
        print(f"warning: could not write {LOG_PATH}: {e}", file=sys.stderr)


def get_policies():
    status, data = call("GET", "/v2/policies")
    if status != 200:
        fail(http_exit_code(status, data), f"GET /v2/policies failed: {status} {data}")
    return data


# ---------- input classification ----------

def classify(raw):
    """Describe an input: source type, expected library id (if knowable), host."""
    raw = raw.strip()
    if re.match(r"^/[^/\s]+(/[^/\s]+)*$", raw):  # already a library id
        return {"input": raw, "kind": "library_id", "libraryId": raw.lower(), "host": None}
    if not re.match(r"^https?://", raw):
        if re.match(r"^[\w.-]+/[\w.-]+$", raw) and "." not in raw.split("/")[0]:
            return classify("https://github.com/" + raw)  # owner/repo shorthand
        return {"input": raw, "kind": "name", "libraryId": None, "host": None}
    u = urllib.parse.urlparse(raw)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    base = host[4:] if host.startswith("www.") else host
    if base in REPO_HOSTS and len(parts) >= 2:
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
        return {"input": raw, "kind": REPO_HOSTS[base], "libraryId": f"/{owner}/{repo}".lower(),
                "host": host, "repoUrl": f"https://{base}/{owner}/{repo}"}
    if base == "github.com" and len(parts) == 1:  # a user or org, not a repo
        return {"input": raw, "kind": "repo_owner", "libraryId": None, "host": host, "owner": parts[0]}
    if base == "context7.com" and len(parts) >= 2:
        return {"input": raw, "kind": "library_id", "libraryId": "/" + "/".join(parts).lower(), "host": None}
    last = parts[-1].lower() if parts else ""
    if re.match(r"^llms(-[\w.-]+)?\.txt$", last):  # llms.txt, llms-full.txt, llms-<section>.txt
        kind = "llmstxt"
    elif last.endswith((".json", ".yaml", ".yml")) and re.search(r"openapi|swagger|api", raw, re.I):
        kind = "openapi"
    elif raw.endswith(".git"):
        kind = "git"
    else:
        kind = "website"
    return {"input": raw, "kind": kind, "libraryId": None, "host": host}


def covering_entries(lib_id, host, allowed):
    """Allowlist entries that cover this library id or docs host."""
    hits = []
    for e in allowed:
        el = e.lower()
        if el.startswith("/"):
            if lib_id and (lib_id == el or lib_id.startswith(el + "/")):
                hits.append(e)
        elif host and (host == el or host.endswith("." + el)):
            hits.append(e)
    return hits


def probe(lib_id):
    """Ask Context7 to serve the library: tells exists / processing / blocked / missing."""
    status, data = call("GET", "/v2/context",
                        params={"libraryId": lib_id, "query": "getting started overview", "type": "json"})
    state = {200: "ready", 202: "processing", 301: "redirected", 403: "blocked_by_policy_or_plan",
             404: "not_found", 422: "unprocessable"}.get(status, f"http_{status}")
    if status == 404 and isinstance(data, dict) and data.get("error") == "no_snippets_found":
        state = "processing_or_empty"  # the library exists; a fresh submit looks like this until parsed
    info = {"probeStatus": status, "state": state}
    if isinstance(data, dict):
        if status != 200:
            info["error"] = data.get("error")
            info["message"] = data.get("message")
        if data.get("redirectUrl"):
            info["redirectUrl"] = data["redirectUrl"]
    return info


def website_id_candidates(url):
    """Guess Context7 ids for a docs URL. Observed shapes: docs.github.com/en -> /websites/github_en,
    docs.astral.sh -> /websites/astral_sh, rfc-editor.org/rfc/ -> /websites/rfc-editor_rfc; a `docs`
    path segment is dropped (example.com/docs/guide -> /websites/example_guide)."""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    for prefix in ("www.", "docs."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    labels = host.split(".")
    if len(labels) > 1 and labels[-1] in ("com", "org", "net"):
        labels = labels[:-1]
    base = "_".join(labels)
    segs = [re.sub(r"\.(html?|md|php)$", "", s.lower()) for s in u.path.split("/") if s]
    segs = [s for s in segs if s not in ("index", "")]
    no_docs = [s for s in segs if s != "docs"]
    seen, out_ids = set(), []
    for path in (no_docs, segs):
        for k in range(len(path), -1, -1):
            cid = "/websites/" + "_".join([base] + path[:k])
            if cid not in seen:
                seen.add(cid)
                out_ids.append(cid)
    return out_ids[:8]


def site_llms_files(url):
    """llms.txt files a docs site publishes (root, docs root, and per-section llms-<section>.txt).
    They list every page, including pages the navigation never links, which crawls miss."""
    u = urllib.parse.urlparse(url)
    root = f"{u.scheme}://{u.netloc}"
    segs = [s for s in u.path.split("/") if s]
    cands = [f"{root}/llms.txt", f"{root}/llms-full.txt"]
    for i in range(1, len(segs) + 1):
        base = root + "/" + "/".join(segs[:i])
        cands += [f"{base}/llms.txt", f"{base}/llms-full.txt"]
        if i >= 2:  # e.g. /docs/guide -> /docs/llms-guide.txt
            cands.append(root + "/" + "/".join(segs[:i - 1]) + f"/llms-{segs[i - 1]}.txt")
    found = []
    for c in dict.fromkeys(cands):
        req = urllib.request.Request(c, method="GET", headers={"User-Agent": "context7-add-skill", "Range": "bytes=0-512"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                ctype = r.headers.get("Content-Type", "")
                if r.status in (200, 206) and "html" not in ctype:
                    found.append(c)
        except (urllib.error.URLError, ValueError):
            pass
    return found


def owner_repos(owner):
    """Public repos of a GitHub user or org (unauthenticated GitHub API, first 100)."""
    req = urllib.request.Request(f"https://api.github.com/users/{urllib.parse.quote(owner)}/repos?per_page=100",
                                 headers={"User-Agent": "context7-add-skill", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return [{"repo": x["name"], "fork": x["fork"], "archived": x["archived"],
                     "stars": x["stargazers_count"]} for x in json.load(r)]
    except (urllib.error.URLError, ValueError) as e:
        return {"error": str(e)}


def search(name, query="documentation"):
    status, data = call("GET", "/v2/libs/search", params={"libraryName": name, "query": query, "fast": "true"})
    if status != 200 or not isinstance(data, dict):
        return {"status": status, "results": [], "error": data}
    keep = ("id", "title", "state", "totalSnippets", "trustScore", "lastUpdateDate")
    return {"status": status, "searchFilterApplied": data.get("searchFilterApplied"),
            "results": [{k: r.get(k) for k in keep} for r in data.get("results", [])[:5]]}


# ---------- commands ----------

def cmd_whoami(_):
    s, ts = call("GET", "/v2/teamspaces")
    s2, usage = call("GET", "/v2/usage")
    pol = get_policies()
    lf = pol.get("libraryFilters", {})
    out({
        "teamspaces": [{k: t.get(k) for k in ("name", "role", "ownerPlan")} for t in (ts or {}).get("teamspaces", [])]
        if s == 200 else ts,
        "plan": usage.get("plan") if s2 == 200 else None,
        "policyMode": lf.get("mode"),
        "allowList": f"{allow_list(lf)[0]}.{allow_list(lf)[1]}" if allow_list(lf) else None,
        "allowListEntries": len(allow_list(lf)[2]) if allow_list(lf) else 0,
        "accessibleLibraryCount": pol.get("accessibleLibraryCount"),
        "sourceTypes": {k: v.get("enabled") for k, v in pol.get("sourceTypes", {}).items()},
    })


def cmd_check(args):
    pol = get_policies()
    lf = pol.get("libraryFilters", {})
    mode = lf.get("mode")
    target = allow_list(lf)
    allowed = target[2] if target else []
    results = []
    for raw in args.inputs:
        c = classify(raw)
        r = dict(c)
        cover = (lambda lib: covering_entries(lib, None, allowed)) if target else (lambda lib: None)
        if c["libraryId"]:
            r.update(probe(c["libraryId"]))
            if r["state"] == "ready":
                r["index"] = library_info(c["libraryId"])
        elif c["kind"] == "repo_owner":
            repos = owner_repos(c["owner"])
            r["note"] = ("a GitHub user/org, not a repo: each repo is its own library. Allow exact repos; "
                         f"an owner entry /{c['owner'].lower()} admits all current and future repos (ask first).")
            if isinstance(repos, dict):
                r["repos"] = repos
            else:
                r["repos"] = []
                for x in repos[:30]:
                    lib = f"/{c['owner']}/{x['repo']}".lower()
                    r["repos"].append({**x, "libraryId": lib, "state": probe(lib)["state"], "coveredBy": cover(lib)})
        else:
            if c["kind"] in ("website", "llmstxt", "openapi"):
                # Search hides libraries the policy refuses, so probe likely ids directly too.
                found = []
                for cid in website_id_candidates(c["input"]):
                    p = probe(cid)
                    if p["state"] != "not_found":
                        hit = {"libraryId": cid, "state": p["state"], "coveredBy": cover(cid)}
                        if p["state"] == "ready":  # how fresh / how big, to judge coverage
                            info = library_info(cid) or {}
                            hit.update({k: info.get(k) for k in ("lastUpdateDate", "totalSnippets")})
                        found.append(hit)
                r["guessedIds"] = found or "none of the usual id shapes exist; `add` reports the id if it does"
                if c["kind"] == "website":
                    r["siteLlmsFiles"] = site_llms_files(c["input"])
            r["searchHint"] = ("search is policy-filtered (refused libraries are hidden). For a name such as "
                               "'RFC 5545', find the canonical source URL and check that instead.")
            r["search"] = search(c["input"] if c["kind"] == "name" else (c["host"] or c["input"]))
            for hit in r["search"].get("results", []):
                hit["coveredBy"] = cover(hit["id"])
        if target:
            # Domain entries match a library's *docs site* host. A repo URL's host (github.com)
            # is not that: a `github.com` entry does not admit arbitrary GitHub repos.
            docs_host = None if c["kind"] in REPO_HOSTS.values() or c["kind"] == "library_id" else c["host"]
            r["coveredBy"] = covering_entries(c["libraryId"], docs_host, allowed)
            if mode == "quality" and not r["coveredBy"]:
                r["coveredBy"] = "not on the wild-card list; may still pass the quality filters (see state)"
        else:
            r["coveredBy"] = "no library filter active: all public libraries allowed"
        results.append(r)
    out({"policyMode": mode, "results": results})


def valid_entry(e):
    return bool(re.match(r"^/[a-z0-9._-]+(/[a-z0-9._@-]+)*$", e) or
                re.match(r"^([a-z0-9-]+\.)+[a-z]{2,}$", e))


def allow_list(lf):
    """Which list admits libraries under the current mode: (mode_key, list_key, entries) or None."""
    mode = lf.get("mode")
    if mode == "select":
        return "select", "allowedLibraries", (lf.get("select") or {}).get("allowedLibraries", [])
    if mode == "quality":  # "Wild Card Libraries": always allowed, even if they fail filters
        return "quality", "exceptedLibraries", (lf.get("quality") or {}).get("exceptedLibraries", [])
    return None


def cmd_allow(args):
    entries = [e.strip().lower() for e in args.entries]
    bad = [e for e in entries if not valid_entry(e)]
    if bad:
        fail(EXIT_USAGE, f"Not a valid allowlist entry (use /org, /org/repo or a domain like docs.example.com): {bad}")
    pol = get_policies()
    lf = pol.get("libraryFilters", {})
    target = allow_list(lf)
    if not target:
        out({"changed": False, "reason": "no library filter is active; every public library is already allowed"})
        return
    mode_key, list_key, existing = target
    current = {e.lower() for e in existing}
    if args.remove:
        todo = [e for e in entries if e in current]
        skipped = [e for e in entries if e not in current]
        op = {"remove": todo}
    else:
        todo = [e for e in entries if e not in current]
        skipped = [e for e in entries if e in current]
        op = {"add": todo}
    if not todo:
        out({"changed": False, "reason": "nothing to do", "alreadyInDesiredState": skipped})
        return
    body = {"libraryFilters": {mode_key: {list_key: op}}}
    if not args.apply:
        body["dryRun"] = True
    status, data = call("PATCH", "/v2/policies", body=body)
    if status != 200:
        fail(http_exit_code(status, data), f"PATCH /v2/policies failed: {status} {data}")
    result = {
        "dryRun": not args.apply,
        "list": f"{mode_key}.{list_key}",
        "operation": op,
        "alreadyInDesiredState": skipped,
        "accessibleLibraryCountBefore": data.get("currentAccessibleLibraryCount", pol.get("accessibleLibraryCount")),
        "accessibleLibraryCountAfter": data.get("accessibleLibraryCount"),
    }
    if args.apply:
        log("allowlist_remove" if args.remove else "allowlist_add", entries=todo,
            countBefore=pol.get("accessibleLibraryCount"), countAfter=data.get("accessibleLibraryCount"))
    out(result)


def prepare_add(url, args):
    """Validate one input and build its request; exits with EXIT_USAGE on bad input."""
    c = classify(url)
    kind = args.type or c["kind"]
    if kind not in ADD_ENDPOINT:
        fail(EXIT_USAGE, f"Cannot tell the source type of {url!r} (got {kind!r}); pass --type "
                 f"{'|'.join(ADD_ENDPOINT)}.")
    if kind == "llmstxt":
        fname = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1].lower()
        if fname not in ("llms.txt", "llms-full.txt", "llms-small.txt"):
            fail(EXIT_USAGE, f"Context7 only accepts files named llms.txt, llms-full.txt or llms-small.txt (got {fname!r}). "
                     "For a per-section file, cover the missing pages with a narrow website source instead: "
                     "add <section url> --base-url <section url>.")
    path, field = ADD_ENDPOINT[kind]
    body = {field: c.get("repoUrl") or url}
    is_repo = field == "docsRepoUrl"
    if args.private:
        if not is_repo:
            fail(EXIT_USAGE, f"--private applies to repositories only (got {url!r}).")
        body["private"] = True
    if args.generate_docs:
        if not (is_repo and args.private):
            fail(EXIT_USAGE, f"--generate-docs applies to private repositories only (got {url!r}).")
        body["generateDocs"] = True
    if args.base_url:
        if kind != "website":
            fail(EXIT_USAGE, f"--base-url applies to websites only (got {url!r}).")
        body["websiteBaseUrl"] = args.base_url
    token_note = None
    if args.git_token_env:
        tok = os.environ.get(args.git_token_env)
        if not tok:
            fail(EXIT_USAGE, f"${args.git_token_env} is empty.")
        if is_repo:
            body["gitToken"] = tok
            token_note = f"gitToken from ${args.git_token_env}"
        else:  # never hand a git token to a website / llms.txt / OpenAPI submission
            token_note = f"${args.git_token_env} not sent: a git token applies to repositories only"
    return {"input": url, "kind": kind, "c": c, "path": path, "field": field, "body": body,
            "tokenNote": token_note}


def submit_add(p, args):
    """POST one prepared add. Returns (result, exit_code, stop) where stop is "queue", "rate" or
    "auth" when the remaining inputs would be refused too."""
    deadline = time.time() + (args.queue_timeout if args.queue_timeout is not None else DEFAULT_QUEUE_TIMEOUT)
    while True:
        status, data = call("POST", p["path"], body=p["body"])
        if not (queue_full(data) and args.queue and time.time() < deadline):
            break
        wait = min(QUEUE_POLL_SEC, max(1, int(deadline - time.time())))
        print(f"queue full (5 libraries processing); retrying {p['input']} in {wait} s", file=sys.stderr)
        time.sleep(wait)
    c = p["c"]
    result = {"input": p["input"], "status": status, "response": data, "expectedLibraryId": c["libraryId"]}
    err = data.get("error") if isinstance(data, dict) else None
    code = 0
    if status == 200:
        d = data if isinstance(data, dict) else {}
        log("add", source=p["kind"], url=p["body"][p["field"]], libraryId=d.get("libraryName"),
            version=d.get("version"), private=bool(args.private))
        result["libraryId"] = d.get("libraryName")
    elif status == 409 or err == "library-already-exists":
        m = re.search(r"(/[\w.@-]+(?:/[\w.@-]+)+)", data.get("message") or "") if isinstance(data, dict) else None
        result["libraryId"] = m.group(1).lower() if m else c["libraryId"]
        result["meaning"] = "already exists in Context7: nothing was created; just make sure it is allowed"
    else:
        code = http_exit_code(status, data)
        if err == QUEUE_FULL:
            result["meaning"] = ("not submitted: Context7 allows 5 libraries processing or queued per user. "
                                 + ("--queue timed out; resubmit later, or rerun with a larger --queue-timeout"
                                    if args.queue else
                                    "resubmit later, or rerun with --queue to wait for a free slot"))
        elif status == 403:
            result["meaning"] = ("refused: if error is access_denied the library id is outside the allowlist "
                                 "(allow it first); otherwise role/plan")
        elif err == "not_documentation_website":
            result["meaning"] = ("refused for good: Context7 doesn't score this as a documentation site. "
                                 "Look for a docs subdomain or an llms.txt, or read it on the web")
        elif status == 429:
            result["meaning"] = "not submitted: rate limited; wait a few minutes, then resubmit"
        elif status == 0:
            result["meaning"] = "network error: Context7 may not have received it; safe to resubmit"
    stop = "queue" if err == QUEUE_FULL else "rate" if status == 429 else "auth" if status == 401 else None
    return result, code, stop


def _sigterm(signum, frame):
    raise KeyboardInterrupt  # a background run that gets killed still prints its partial summary


def cmd_add(args):
    many = len(args.urls) > 1
    if many and args.base_url:
        fail(EXIT_USAGE, "--base-url applies to one website at a time; run add once per website.")
    if many and args.type:
        fail(EXIT_USAGE, "--type applies to one URL at a time; leave it out (the type is detected per URL) "
                         "or run add once per URL that needs it.")
    if args.queue_timeout is not None and not args.queue:
        fail(EXIT_USAGE, "--queue-timeout only makes sense with --queue.")
    if args.queue_timeout is not None and args.queue_timeout < 0:
        fail(EXIT_USAGE, "--queue-timeout must be 0 or more seconds.")
    plans = [prepare_add(u, args) for u in args.urls]  # validate everything before submitting anything
    if not args.apply:
        dry = [{"dryRun": True, "input": p["input"], "would": f"POST {API}{p['path']}",
                "body": {k: ("<redacted>" if k == "gitToken" else v) for k, v in p["body"].items()},
                "expectedLibraryId": p["c"]["libraryId"], "note": p["tokenNote"]} for p in plans]
        out({"dryRun": True, "results": dry} if many else dry[0])
        return
    previous = signal.signal(signal.SIGTERM, _sigterm)
    results, failed, not_submitted, stopped, interrupted = [], [], [], None, False
    first_failure, i = 0, 0
    try:
        for i, p in enumerate(plans):
            result, code, stop = submit_add(p, args)
            results.append(result)
            if stop:  # recorded before anything else, so an interrupt during the print keeps it
                not_submitted = [q["input"] for q in plans[i:]]  # this URL too: it never got through
                stopped = stop
            resp = result["response"]
            what = result.get("libraryId") or (resp.get("error") if isinstance(resp, dict) else "") or ""
            print(f"[{i + 1}/{len(plans)}] {p['input']}: {result['status']} {what}", file=sys.stderr)
            if stop:  # the queue won't drain mid-loop, a rate limit won't lift, a bad key stays bad
                break
            if code:
                failed.append(p["input"])
                first_failure = first_failure or code
    except KeyboardInterrupt:
        interrupted = True
        if not stopped:  # by position, so duplicates are kept; the URL in flight may have been accepted
            not_submitted = [q["input"] for q in plans[len(results):]]
    finally:
        signal.signal(signal.SIGTERM, previous)  # a second signal now stops the process as usual
    if interrupted:  # the run is incomplete: say so before anything else
        exit_code = EXIT_INTERRUPTED
    elif first_failure:  # a real refusal matters more than "try again later"
        exit_code = first_failure
    elif stopped == "auth":
        exit_code = EXIT_AUTH
    elif stopped == "queue":
        exit_code = EXIT_QUEUE
    elif stopped == "rate":
        exit_code = EXIT_RATE
    else:
        exit_code = 0
    if not many and not interrupted:
        out(results[0])
        sys.exit(exit_code)
    summary = {"results": results}
    if failed:
        summary["failed"] = failed
    if not_submitted:
        summary["notSubmitted"] = not_submitted
    if interrupted:
        summary["interrupted"] = True
        summary["next"] = ("stopped before finishing: resubmit notSubmitted (safe even if the first one "
                           "was already accepted: Context7 answers library-already-exists)")
    elif stopped == "queue":
        summary["next"] = ("Context7's queue holds 5 libraries per user. Resubmit notSubmitted once some "
                           "finish (`wait <id>`), or rerun add with --queue (run it in the background).")
    elif stopped == "rate":
        summary["next"] = "rate limited: wait a few minutes, then resubmit notSubmitted"
    elif stopped == "auth":
        summary["next"] = "the API key was refused (401): fix the key, then resubmit notSubmitted"
    out(summary)
    sys.exit(exit_code)


def library_info(lib_id):
    """state / lastUpdateDate / snippet count from search (only for libraries the policy allows)."""
    name = lib_id.rstrip("/").split("/")[-1]
    status, data = call("GET", "/v2/libs/search", params={"libraryName": name, "query": "documentation"})
    for r in (data.get("results", []) if isinstance(data, dict) else []):
        if r.get("id", "").lower() == lib_id:
            return {k: r.get(k) for k in ("id", "title", "state", "lastUpdateDate", "totalSnippets", "totalTokens")}
    return None


def cmd_refresh(args):
    lib_id = args.library_id.lower()
    before = library_info(lib_id)
    if not args.apply:
        out({"dryRun": True, "would": f"POST {API}/v1/refresh", "body": {"libraryName": lib_id},
             "current": before, "note": "re-crawls / re-parses the source; a daily refresh limit applies"})
        return
    status, data = call("POST", "/v1/refresh", body={"libraryName": lib_id})
    if status == 200:
        log("refresh", libraryId=lib_id, before=before)
    out({"status": status, "response": data, "before": before,
         "next": "run `wait --changed` to follow it until lastUpdateDate moves"})
    if status != 200:
        sys.exit(http_exit_code(status, data))


def multipart(fields, file_field, file_path):
    boundary = "----c7" + uuid.uuid4().hex
    lines = []
    for k, v in fields.items():
        lines += [f"--{boundary}", f'Content-Disposition: form-data; name="{k}"', "", str(v)]
    head = "\r\n".join(lines + [f"--{boundary}",
                                f'Content-Disposition: form-data; name="{file_field}"; '
                                f'filename="{os.path.basename(file_path)}"',
                                f"Content-Type: {mimetypes.guess_type(file_path)[0] or 'application/octet-stream'}",
                                "", ""]).encode()
    with open(file_path, "rb") as f:
        payload = f.read()
    return head + payload + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def size_hint(ext, size):
    how = {".pdf": "Split it by pages and upload each part, e.g. `qpdf --empty --pages in.pdf 1-3 -- in-p1-3.pdf`; "
                   "put the page range in the file name and --title.",
           ".docx": "Split the document into smaller files (or remove large images) and upload each part."}.get(
        ext, "Split the spec into smaller files (for example by tag) or remove large embedded examples.")
    return (f"The file is {size:,} bytes ({size / 1e6:.2f} MB); Context7 refuses request bodies over about 4.5 MB in practice "
            f"(413 FUNCTION_PAYLOAD_TOO_LARGE), and this script refuses files over {MAX_UPLOAD_BYTES:,} bytes to leave "
            f"room for the form envelope. {how}")


def cmd_upload(args):
    ext = os.path.splitext(args.file)[1].lower()
    if ext not in UPLOAD_ENDPOINT:
        fail(EXIT_USAGE, f"Unsupported file type {ext!r}: use .pdf, .docx, or an OpenAPI .json/.yaml/.yml.")
    if not os.path.isfile(args.file):
        fail(EXIT_USAGE, f"No such file: {args.file}")
    path, file_field, title_field = UPLOAD_ENDPOINT[ext]
    fields = {}
    if args.title:
        fields[title_field] = args.title
    if args.description:
        fields["description"] = args.description
    size = os.path.getsize(args.file)
    too_big = size > MAX_UPLOAD_BYTES
    split_hint = size_hint(ext, size)
    if not args.apply:
        dry = {"dryRun": True, "would": f"POST {API}{path}", "file": args.file, "bytes": size, "fields": fields,
               "note": "Uploaded files are private to the teamspace (source type uploaded_files), "
                       "not subject to the public allowlist. The library id is built from the file name "
                       "(for a PDF: /pdf/<prefix>-<file name>-<hash>), not from --title: rename the file "
                       "first if its name says nothing."}
        if too_big:
            dry["sizeWarning"] = split_hint
        out(dry)
        return
    if too_big and not args.skip_size_check:
        fail(EXIT_USAGE, split_hint + " Nothing was sent. If Context7 raised the limit, rerun with --skip-size-check.")
    status, data = call("POST", path, form=multipart(fields, file_field, args.file))
    result = {"status": status, "response": data}
    if status == 200 and isinstance(data, dict):
        log("upload", file=os.path.basename(args.file), libraryId=data.get("libraryName"))
        result["libraryId"] = data.get("libraryName")
    elif status == 413:
        result["meaning"] = split_hint
    elif status == 0:
        result["meaning"] = "network error: check the connection and retry"
        if size > LARGE_UPLOAD_BYTES:  # a big body can also be cut off by the size limit
            result["meaning"] += ". The file is large, so Context7 may have refused it for size: " + split_hint
    out(result)
    if status != 200:
        sys.exit(http_exit_code(status, data))


def cmd_wait(args):
    lib_id = args.library_id.lower()
    deadline = time.time() + args.timeout
    if args.changed:  # after a refresh: the old content stays "ready", so watch lastUpdateDate instead
        start = library_info(lib_id) or {}
        print(f"waiting for {lib_id} to change from lastUpdateDate={start.get('lastUpdateDate')}", file=sys.stderr)
        while time.time() < deadline:
            time.sleep(args.interval)
            now = library_info(lib_id) or {}
            if now.get("lastUpdateDate") != start.get("lastUpdateDate") and now.get("state") in ("finalized", None):
                out({"libraryId": lib_id, "changed": True, "before": start, "after": now})
                return
        out({"libraryId": lib_id, "changed": False, "timedOut": True, "current": library_info(lib_id)})
        sys.exit(EXIT_TIMEOUT)
    last = None
    while True:
        info = probe(lib_id)
        if info["state"] != last:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {lib_id}: {info['state']}"
                  + (f" ({info.get('error')}: {info.get('message')})" if info.get("error") else ""),
                  file=sys.stderr)
            last = info["state"]
        done = info["state"] in ("ready", "redirected", "unprocessable", "blocked_by_policy_or_plan")
        if done or time.time() >= deadline:
            info["libraryId"] = lib_id
            info["timedOut"] = not done
            out(info)
            if not done:
                sys.exit(EXIT_TIMEOUT)
            sys.exit({"blocked_by_policy_or_plan": EXIT_POLICY, "unprocessable": EXIT_UNPROCESSABLE}.get(info["state"], 0))
        time.sleep(args.interval)


def cmd_log(_):
    if not os.path.exists(LOG_PATH):
        print("nothing logged yet", file=sys.stderr)  # stdout stays JSON lines (here: none)
        return
    with open(LOG_PATH) as f:
        sys.stdout.write(f.read())


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    apply_help = "make the change; without it the command is a dry run that only prints what it would do"

    def cmd(name, purpose):
        return sub.add_parser(name, help=purpose, description=purpose + " Exit codes: see `c7.py --help`.")

    s = cmd("whoami", "Show plan, role, policy mode and which list admits libraries.")
    s.set_defaults(fn=cmd_whoami)
    s = cmd("check", "Report whether each input is indexed, fresh and allowed by the teamspace policy.")
    s.add_argument("inputs", nargs="+",
                   help="repo URL, owner/repo, library id, context7.com URL, docs/llms.txt/OpenAPI URL, "
                        "GitHub user/org URL, or a name")
    s.set_defaults(fn=cmd_check)
    s = cmd("allow", "Add or remove allowlist / wild-card entries.")
    s.add_argument("entries", nargs="+", help="/owner/repo (one library), /owner (all of it), or a docs domain")
    s.add_argument("--remove", action="store_true", help="remove the entries instead of adding them")
    s.add_argument("--apply", action="store_true", help=apply_help)
    s.set_defaults(fn=cmd_allow)
    s = cmd("add", "Submit repos, websites, llms.txt or OpenAPI URLs for indexing.")
    s.add_argument("urls", nargs="+", metavar="url", help="one or more sources; all are validated before any is sent")
    s.add_argument("--type", choices=list(ADD_ENDPOINT),
                   help="force the source type when it can't be detected (one URL only)")
    s.add_argument("--private", action="store_true", help="index a private repository (paid plan)")
    s.add_argument("--generate-docs", action="store_true",
                   help="private repositories only: let Context7 generate docs from the code")
    s.add_argument("--base-url", help="websites only: crawl only pages under this URL (one URL only)")
    s.add_argument("--git-token-env", metavar="VAR",
                   help="name of an env var holding a git token for repositories (never pass the token itself)")
    s.add_argument("--queue", action="store_true",
                   help="when the 5-submission queue is full, wait and retry instead of stopping")
    s.add_argument("--queue-timeout", type=int, default=None, metavar="SEC",
                   help=f"with --queue, per URL: give up waiting for a free slot after SEC seconds "
                        f"(default {DEFAULT_QUEUE_TIMEOUT})")
    s.add_argument("--apply", action="store_true", help=apply_help)
    s.set_defaults(fn=cmd_add)
    s = cmd("upload", "Upload a PDF, DOCX or OpenAPI file (private to the teamspace, max ~4.5 MB).")
    s.add_argument("file", help="a .pdf, .docx, or OpenAPI .json/.yaml/.yml file")
    s.add_argument("--title", help="library display name (the id still comes from the file name)")
    s.add_argument("--description", help="short summary of the document")
    s.add_argument("--skip-size-check", action="store_true",
                   help="send a file over the ~4.5 MB limit anyway (only if Context7 raised the limit)")
    s.add_argument("--apply", action="store_true", help=apply_help)
    s.set_defaults(fn=cmd_upload)
    s = cmd("refresh", "Re-crawl or re-parse a stale library (daily limit).")
    s.add_argument("library_id", help="library id, e.g. /owner/repo")
    s.add_argument("--apply", action="store_true", help=apply_help)
    s.set_defaults(fn=cmd_refresh)
    s = cmd("wait", "Poll until a library is indexed and served.")
    s.add_argument("library_id", help="library id, e.g. /owner/repo")
    s.add_argument("--timeout", type=int, default=900, metavar="SEC", help="give up after SEC seconds (default 900)")
    s.add_argument("--interval", type=int, default=20, metavar="SEC", help="seconds between polls (default 20)")
    s.add_argument("--changed", action="store_true", help="after a refresh: wait until lastUpdateDate moves")
    s.set_defaults(fn=cmd_wait)
    s = cmd("log", "Print what this skill changed before (registry.jsonl, one JSON object per line).")
    s.set_defaults(fn=cmd_log)
    return p


def main():
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
