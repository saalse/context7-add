#!/usr/bin/env python3
"""Context7 add + allowlist helper (stdlib only).
Part of the context7-add skill by Alex Samson (https://saalse.com), MIT license.

Every command that changes something is a dry run unless --apply is passed.

  c7.py whoami                         plan, teamspace, role, policy mode, allowlist size
  c7.py check <url|/org/repo|name>...  is it in Context7? is it allowed by the policy?
  c7.py allow <entry>... [--remove] [--apply]
                                       add/remove allowlist entries (/org, /org/repo, domain.com)
  c7.py add <url> [--type T] [--private] [--generate-docs] [--base-url URL]
              [--git-token-env VAR] [--apply]
                                       submit a repo / website / llms.txt / OpenAPI URL
  c7.py upload <file> [--title T] [--description D] [--apply]
                                       upload a PDF, DOCX or OpenAPI file
  c7.py refresh <libraryId> [--apply]  re-crawl / re-parse a stale library (daily limit)
  c7.py wait <libraryId> [--changed] [--timeout SEC]
                                       poll until the library is indexed and served;
                                       --changed: until a refresh lands (lastUpdateDate moves)
  c7.py log                            show what this skill has changed before

Examples:
  c7.py check https://github.com/acme/widgets https://docs.acme.dev/
  c7.py allow /acme/widgets --apply
  c7.py add https://docs.acme.dev/guide/ --base-url https://docs.acme.dev/guide/ --apply

API key: $CONTEXT7_API_KEY, else the context7 MCP entry in ~/.claude.json.
The key is never printed. Results are JSON on stdout; progress and errors on stderr.

Exit codes:
  0  success (check: the report was produced, read its fields; wait: ready)
  1  API or network error
  2  bad input (arguments, file type, unsupported llms.txt name)
  3  auth: missing/invalid API key, or role/plan too low
  4  library not found
  5  refused by the teamspace policy (access_denied)
  6  rate limited (429)
  7  library unprocessable (no or too few docs)
  8  wait timed out
"""
import argparse
import json
import mimetypes
import os
import re
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
EXIT_POLICY, EXIT_RATE, EXIT_UNPROCESSABLE, EXIT_TIMEOUT = 5, 6, 7, 8


def fail(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def http_exit_code(status, data):
    err = data.get("error") if isinstance(data, dict) else None
    if status == 403:
        return EXIT_POLICY if err == "access_denied" else EXIT_AUTH
    return {0: EXIT_API, 400: EXIT_USAGE, 401: EXIT_AUTH, 404: EXIT_NOT_FOUND,
            422: EXIT_UNPROCESSABLE, 429: EXIT_RATE}.get(status, EXIT_API)

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
        status, raw = e.code, e.read()
    except urllib.error.URLError as e:
        return 0, {"error": "network", "message": str(e.reason)}
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def log(action, **fields):
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, **fields}
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
    nianticspatial.com/docs/scaniverse -> /websites/nianticspatial_scaniverse, docs.astral.sh ->
    /websites/astral_sh, rfc-editor.org/rfc/ -> /websites/rfc-editor_rfc."""
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
        if i >= 2:  # e.g. /docs/scaniverse -> /docs/llms-scaniverse.txt
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


def cmd_add(args):
    c = classify(args.url)
    kind = args.type or c["kind"]
    if kind not in ADD_ENDPOINT:
        fail(EXIT_USAGE, f"Cannot tell the source type of {args.url!r} (got {kind!r}); pass --type "
                 f"{'|'.join(ADD_ENDPOINT)}.")
    if kind == "llmstxt":
        fname = urllib.parse.urlparse(args.url).path.rstrip("/").split("/")[-1].lower()
        if fname not in ("llms.txt", "llms-full.txt", "llms-small.txt"):
            fail(EXIT_USAGE, f"Context7 only accepts files named llms.txt, llms-full.txt or llms-small.txt (got {fname!r}). "
                     "For a per-section file, cover the missing pages with a narrow website source instead: "
                     "add <section url> --base-url <section url>.")
    path, field = ADD_ENDPOINT[kind]
    body = {field: c.get("repoUrl") or args.url}
    is_repo = field == "docsRepoUrl"
    if args.private:
        if not is_repo:
            fail(EXIT_USAGE, "--private applies to repositories only.")
        body["private"] = True
    if args.generate_docs:
        if not (is_repo and args.private):
            fail(EXIT_USAGE, "--generate-docs applies to private repositories only.")
        body["generateDocs"] = True
    if args.base_url:
        if kind != "website":
            fail(EXIT_USAGE, "--base-url applies to websites only.")
        body["websiteBaseUrl"] = args.base_url
    token_note = None
    if args.git_token_env:
        tok = os.environ.get(args.git_token_env)
        if not tok:
            fail(EXIT_USAGE, f"${args.git_token_env} is empty.")
        body["gitToken"] = tok
        token_note = f"gitToken from ${args.git_token_env}"
    shown = {k: ("<redacted>" if k == "gitToken" else v) for k, v in body.items()}
    if not args.apply:
        out({"dryRun": True, "would": f"POST {API}{path}", "body": shown,
             "expectedLibraryId": c["libraryId"], "note": token_note})
        return
    status, data = call("POST", path, body=body)
    result = {"status": status, "response": data, "expectedLibraryId": c["libraryId"]}
    if status == 200 and isinstance(data, dict):
        log("add", source=kind, url=body[field], libraryId=data.get("libraryName"),
            version=data.get("version"), private=bool(args.private))
        result["libraryId"] = data.get("libraryName")
    elif status == 409 or (isinstance(data, dict) and data.get("error") == "library-already-exists"):
        m = re.search(r"(/[\w.@-]+(?:/[\w.@-]+)+)", (data or {}).get("message", "")) if isinstance(data, dict) else None
        result["libraryId"] = m.group(1).lower() if m else c["libraryId"]
        result["meaning"] = "already exists in Context7: nothing was created; just make sure it is allowed"
        status = 409
    elif status == 403:
        result["meaning"] = ("refused: if error is access_denied the library id is outside the allowlist "
                             "(allow it first); otherwise role/plan")
    out(result)
    if status not in (200, 409):
        sys.exit(http_exit_code(status, data))


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
    if not args.apply:
        out({"dryRun": True, "would": f"POST {API}{path}", "file": args.file, "bytes": size, "fields": fields,
             "note": "Uploaded files are private to the teamspace (source type uploaded_files), "
                     "not subject to the public allowlist."})
        return
    status, data = call("POST", path, form=multipart(fields, file_field, args.file))
    if status == 200 and isinstance(data, dict):
        log("upload", file=os.path.basename(args.file), libraryId=data.get("libraryName"))
    out({"status": status, "response": data})
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
        print("(nothing logged yet)")
        return
    with open(LOG_PATH) as f:
        sys.stdout.write(f.read())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    s = sub.add_parser("check"); s.add_argument("inputs", nargs="+"); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("allow"); s.add_argument("entries", nargs="+")
    s.add_argument("--remove", action="store_true"); s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_allow)
    s = sub.add_parser("add"); s.add_argument("url")
    s.add_argument("--type", choices=list(ADD_ENDPOINT)); s.add_argument("--private", action="store_true")
    s.add_argument("--generate-docs", action="store_true"); s.add_argument("--base-url")
    s.add_argument("--git-token-env", help="name of an env var holding a git token (never pass the token itself)")
    s.add_argument("--apply", action="store_true"); s.set_defaults(fn=cmd_add)
    s = sub.add_parser("upload"); s.add_argument("file"); s.add_argument("--title"); s.add_argument("--description")
    s.add_argument("--apply", action="store_true"); s.set_defaults(fn=cmd_upload)
    s = sub.add_parser("refresh"); s.add_argument("library_id"); s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_refresh)
    s = sub.add_parser("wait"); s.add_argument("library_id")
    s.add_argument("--timeout", type=int, default=900); s.add_argument("--interval", type=int, default=20)
    s.add_argument("--changed", action="store_true", help="after a refresh: wait until lastUpdateDate moves")
    s.set_defaults(fn=cmd_wait)
    sub.add_parser("log").set_defaults(fn=cmd_log)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
