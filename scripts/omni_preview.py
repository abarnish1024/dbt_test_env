#!/usr/bin/env python3
"""Create or tear down a per-PR Omni preview environment for dbt changes.

preview:
  1. Create/reuse a shared Omni dbt environment pointed at schema dbt_pr_<N>
  2. Create/reuse an Omni model branch named after the git branch
  3. Point that branch at the dbt env + dbt git branch
  4. dbt-sync + schema-refresh the Omni branch
  5. Rewrite dbt views so they query dbt_pr_<N> and the PR's columns
  6. Run the content validator (including personal folders)
  7. If --autofix (PR label omni-autofix): find/replace 1:1 column
     renames on the Omni branch and git-commit an Omni PR
  8. Comment on the GitHub PR (when GITHUB_TOKEN is set)

cleanup:
  Drop MotherDuck schema dbt_pr_<N> and delete the Omni dbt environment.
  Leaves the Omni model branch / git PR so you can still merge YAML fixes.

refresh-prod:
  Schema-refresh the shared model (no branch) after dbt prod has landed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMMENT_MARKER = "<!-- omni-preview-ci -->"
AUTOFIX_LABEL = "omni-autofix"


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def require(name: str) -> str:
    value = env(name)
    if not value:
        raise SystemExit(f"Missing required environment variable {name}")
    return value


def base_url() -> str:
    return env("OMNI_BASE_URL", "https://andrewbarnish.omniapp.co").rstrip("/")


def api_key() -> str:
    return require("OMNI_API_KEY")


def connection_id() -> str:
    return env("OMNI_CONNECTION_ID", "51e3303e-1e8a-4752-82f9-419eed9b4cba")


def model_id() -> str:
    return env("OMNI_MODEL_ID", "aa9824df-0ccd-4a92-8954-d56203363e48")


def md_database() -> str:
    return env("MOTHERDUCK_DATABASE", "my_db")


def pr_schema(pr_number: int) -> str:
    return f"dbt_pr_{pr_number}"


def prod_schema() -> str:
    return env("OMNI_PROD_SCHEMA", "main")


def env_name(pr_number: int) -> str:
    return f"ci-pr-{pr_number}"


def omni_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    url = base_url() + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < 5:
                retry_after = exc.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                print(f"Omni rate limited; retrying in {wait_s:.0f}s")
                time.sleep(wait_s)
                last_error = exc
                continue
            raise RuntimeError(f"Omni {method} {path} -> HTTP {exc.code}: {detail}") from exc
    raise RuntimeError(f"Omni {method} {path} still rate limited") from last_error


def list_all_models(include: str | None = "activeBranches") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        query: dict[str, str] = {}
        if include:
            query["include"] = include
        if cursor:
            query["cursor"] = cursor
        payload = omni_request("GET", "/api/v1/models", query=query or None)
        if isinstance(payload, list):
            return payload
        batch = payload.get("records") or payload.get("models") or []
        records.extend(batch)
        page = payload.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return records


def find_branch(git_branch: str) -> dict[str, Any] | None:
    wanted = git_branch.strip()
    for model in list_all_models():
        kind = (model.get("modelKind") or model.get("kind") or "").upper()
        name = model.get("name") or ""
        if kind == "BRANCH" and name == wanted:
            return model
        for branch in model.get("branches") or []:
            if (branch.get("name") or "") == wanted:
                merged = dict(branch)
                merged.setdefault("name", wanted)
                return merged
    return None


def create_or_get_branch(git_branch: str) -> dict[str, Any]:
    existing = find_branch(git_branch)
    if existing:
        print(f"Reusing Omni branch {git_branch} ({existing.get('id')})")
        return existing
    payload = omni_request(
        "POST",
        "/api/v1/models",
        {
            "connectionId": connection_id(),
            "modelKind": "BRANCH",
            "modelName": git_branch,
            "baseModelId": model_id(),
        },
    )
    model = payload.get("model") or payload
    if not model.get("id"):
        existing = find_branch(git_branch)
        if existing:
            return existing
        raise RuntimeError(f"Could not create Omni branch: {payload}")
    print(f"Created Omni branch {git_branch} ({model.get('id')})")
    return model


def list_dbt_environments() -> list[dict[str, Any]]:
    payload = omni_request(
        "GET",
        f"/api/v1/connections/{connection_id()}/dbt/environments",
    )
    if isinstance(payload, list):
        return payload
    return payload.get("records") or []


def create_or_update_dbt_env(pr_number: int) -> dict[str, Any]:
    name = env_name(pr_number)
    schema = pr_schema(pr_number)
    existing = next((item for item in list_dbt_environments() if item.get("name") == name), None)
    body = {
        "name": name,
        "targetSchema": schema,
        "targetDatabase": md_database(),
        "ownerId": None,
        "isDeferralEnabled": False,
    }
    if existing:
        env_id = existing["id"]
        updated = omni_request(
            "PUT",
            f"/api/v1/connections/{connection_id()}/dbt/environments/{env_id}",
            {
                "name": name,
                "targetSchema": schema,
                "targetDatabase": md_database(),
            },
        )
        print(f"Updated Omni dbt env {name} ({env_id}) -> schema {schema}")
        return updated if updated.get("id") else {**existing, **body, "id": env_id}
    created = omni_request(
        "POST",
        f"/api/v1/connections/{connection_id()}/dbt/environments",
        body,
    )
    print(f"Created Omni dbt env {name} ({created.get('id')}) -> schema {schema}")
    return created


def set_branch_dbt(git_branch: str, dbt_environment_id: str) -> None:
    encoded = urllib.parse.quote(git_branch, safe="")
    omni_request(
        "POST",
        f"/api/v1/models/{model_id()}/branch/{encoded}/dbt",
        {
            "dbt_environment_id": dbt_environment_id,
            "dbt_git_branch": git_branch,
        },
    )
    print(f"Pointed Omni branch {git_branch} at dbt env {dbt_environment_id} / git {git_branch}")


def poll_job(job_id: str, timeout_s: int = 600, label: str = "Omni job") -> None:
    deadline = time.time() + timeout_s
    while True:
        payload = omni_request("GET", f"/api/v1/jobs/{job_id}/status")
        status = (payload.get("status") or "").upper()
        print(f"{label} {job_id}: {status or payload}")
        if status in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
            return
        if status in {"FAILED", "ERROR", "CANCELLED"}:
            raise RuntimeError(f"{label} failed: {payload}")
        if time.time() > deadline:
            raise RuntimeError(f"Timed out waiting for {label} {job_id}")
        time.sleep(4)


def refresh_schema(branch_id: str | None) -> None:
    query = {"hard_refresh": "true"}
    if branch_id:
        query["branch_id"] = branch_id
    payload = omni_request(
        "POST",
        f"/api/v1/models/{model_id()}/refresh",
        query=query,
    )
    job_id = payload.get("jobId") or payload.get("job_id")
    if not job_id:
        raise RuntimeError(f"Schema refresh did not return a job id: {payload}")
    poll_job(job_id, label="Schema refresh")


def dbt_sync(branch_id: str) -> None:
    payload = omni_request(
        "POST",
        f"/api/v1/models/{model_id()}/dbt-sync",
        query={"branch_id": branch_id},
    )
    job_id = payload.get("jobId") or payload.get("job_id")
    if not job_id:
        raise RuntimeError(f"dbt-sync did not return a job id: {payload}")
    poll_job(job_id, label="dbt-sync")


def ci_table_columns(schema: str) -> dict[str, list[str]]:
    token = env("MOTHERDUCK_TOKEN")
    if not token:
        print("MOTHERDUCK_TOKEN not set; skip aligning Omni views to CI schema")
        return {}
    try:
        import duckdb
    except ImportError:
        print("duckdb not installed; skip aligning Omni views to CI schema")
        return {}
    con = duckdb.connect(f"md:{md_database()}?motherduck_token={token}")
    rows = con.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = ?
        ORDER BY table_name, ordinal_position
        """,
        [schema],
    ).fetchall()
    tables: dict[str, list[str]] = {}
    for table_name, column_name in rows:
        tables.setdefault(str(table_name), []).append(str(column_name))
    return tables


def _yaml_scalar(value: str) -> str:
    if value == "" or any(ch in value for ch in ":#{}[]&*?|>!%@`'\"\n"):
        return json.dumps(value)
    return value


def _dimension_block(name: str, body: Any) -> str:
    cfg = body if isinstance(body, dict) else {}
    description = cfg.get("description")
    fmt = cfg.get("format")
    ignored = cfg.get("ignored")
    if ignored is True:
        return f"  {name}:\n    ignored: true\n"
    if not description and not fmt:
        return f"  {name}: {{}}\n"
    lines = [f"  {name}:\n"]
    if fmt:
        lines.append(f"    format: {fmt}\n")
    if description:
        lines.append(f"    description: {_yaml_scalar(str(description))}\n")
    return "".join(lines)


def rewrite_view_for_ci(
    file_name: str,
    yaml_text: str,
    view_name: str,
    schema: str,
    columns: list[str],
) -> str | None:
    schema_match = re.search(r"^schema:\s*(\S+)", yaml_text, re.M)
    table_match = re.search(r"^table_name:\s*(\S+)", yaml_text, re.M)
    if not table_match:
        return None
    current_schema = schema_match.group(1) if schema_match else ""
    dim_names: list[str] = []
    in_dims = False
    for line in yaml_text.splitlines():
        if line.startswith("dimensions:"):
            in_dims = True
            continue
        if in_dims:
            if line.startswith("measures:") or line.startswith("dbt:") or (
                line and not line.startswith((" ", "#"))
            ):
                break
            match = re.match(r"^  ([A-Za-z_][A-Za-z0-9_]*):", line)
            if match:
                dim_names.append(match.group(1))
    existing_dims: dict[str, Any] = {}
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(yaml_text) or {}
        if isinstance(parsed.get("dimensions"), dict):
            existing_dims = parsed["dimensions"]
        description = parsed.get("description") or ""
        measures = parsed.get("measures") if isinstance(parsed.get("measures"), dict) else {}
    except Exception:
        parsed = {}
        description = ""
        desc_match = re.search(r"^description:\s*(.+)$", yaml_text, re.M)
        if desc_match:
            description = desc_match.group(1).strip().strip("\"'")
        measures = {}
    stale = [name for name in dim_names if name not in columns]
    already_ignored = {
        name
        for name in stale
        if isinstance(existing_dims.get(name), dict) and existing_dims[name].get("ignored") is True
    }
    if (
        current_schema == schema
        and set(columns).issubset(set(dim_names))
        and set(stale) == already_ignored
    ):
        return None
    if not measures:
        measures = {"count": {"aggregate_type": "count"}}
    parts = [f"# Reference this view as {view_name}\n"]
    if description:
        parts.append(f"description: {_yaml_scalar(str(description))}\n\n")
    parts.append(f"schema: {schema}\n")
    parts.append(f"table_name: {table_match.group(1)}\n\n")
    parts.append("dimensions:\n")
    for column in columns:
        parts.append(_dimension_block(column, existing_dims.get(column)))
    for name in stale:
        parts.append(_dimension_block(name, {"ignored": True}))
        print(f"Ignoring stale Omni field {view_name}.{name} (not in CI table)")
    parts.append("\nmeasures:\n")
    for name, body in measures.items():
        cfg = body if isinstance(body, dict) else {}
        agg = cfg.get("aggregate_type")
        if agg:
            parts.append(f"  {name}:\n    aggregate_type: {agg}\n")
        else:
            parts.append(f"  {name}: {{}}\n")
    return "".join(parts)


def dbt_model_tables() -> set[str]:
    return {path.stem for path in (ROOT / "models").rglob("*.sql")}


def align_views_to_schema(schema: str, branch_id: str | None) -> None:
    tables = {
        name: cols
        for name, cols in ci_table_columns(schema).items()
        if name in dbt_model_tables()
    }
    if not tables:
        print(f"No dbt model tables found in {schema}; skip view rewrite")
        return
    query: dict[str, str] = {}
    if branch_id:
        query["branchId"] = branch_id
    listing = omni_request(
        "GET",
        f"/api/v1/models/{model_id()}/yaml",
        query=query or None,
    )
    view_names = listing.get("viewNames") or {}
    rewritten = 0
    for file_name, view_name in view_names.items():
        if not str(file_name).endswith(".view") or ".query.view" in str(file_name):
            continue
        if str(file_name).startswith("omni_dbt/"):
            continue
        if not any(table in str(file_name) or table in str(view_name) for table in tables):
            continue
        get_query = {"fileName": str(file_name)}
        if branch_id:
            get_query["branchId"] = branch_id
        payload = omni_request(
            "GET",
            f"/api/v1/models/{model_id()}/yaml",
            query=get_query,
        )
        files = payload.get("files") or {}
        if not files:
            continue
        actual_name = next(iter(files.keys()))
        yaml_text = files.get(file_name) or files[actual_name]
        if not yaml_text:
            continue
        table_match = re.search(r"^table_name:\s*(\S+)", yaml_text, re.M)
        if not table_match or table_match.group(1) not in tables:
            continue
        table = table_match.group(1)
        new_yaml = rewrite_view_for_ci(
            str(actual_name),
            yaml_text,
            str(view_name or table),
            schema,
            tables[table],
        )
        if not new_yaml:
            print(f"View {actual_name} already points at {schema}")
            continue
        dest_name = str(actual_name)
        if schema == prod_schema():
            dest_name = f"main/{table}.view"
        body: dict[str, Any] = {
            "fileName": dest_name,
            "yaml": new_yaml,
            "mode": "extension",
            "commitMessage": f"Point {dest_name} at schema {schema}",
        }
        if branch_id:
            body["branchId"] = branch_id
        try:
            omni_request(
                "POST",
                f"/api/v1/models/{model_id()}/yaml",
                body,
            )
        except Exception as exc:
            print(f"Could not write {dest_name}: {exc}")
            continue
        if dest_name != actual_name:
            delete_query = {"fileName": actual_name, "commitMessage": f"Remove CI path {actual_name}"}
            if branch_id:
                delete_query["branchId"] = branch_id
            try:
                omni_request(
                    "DELETE",
                    f"/api/v1/models/{model_id()}/yaml",
                    query=delete_query,
                )
                print(f"Deleted stale {actual_name}")
            except Exception as exc:
                print(f"Could not delete {actual_name}: {exc}")
        rewritten += 1
        print(f"Rewrote {actual_name} -> {dest_name} schema {schema} columns {tables[table]}")
    print(f"Aligned {rewritten} Omni view(s) to {schema}")


def align_views_to_ci_schema(pr_number: int, branch_id: str) -> None:
    align_views_to_schema(pr_schema(pr_number), branch_id)


def list_view_names(branch_id: str) -> dict[str, Any]:
    listing = omni_request(
        "GET",
        f"/api/v1/models/{model_id()}/yaml",
        query={"branchId": branch_id},
    )
    return listing.get("viewNames") or {}


def dbt_sql_aliases(table: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for path in (ROOT / "models").rglob(f"{table}.sql"):
        for old, new in re.findall(
            r"(?:[\w]+\.)?([A-Za-z_][\w]*)\s+as\s+([A-Za-z_]\w*)",
            path.read_text(),
            flags=re.I,
        ):
            if old != new:
                aliases[old] = new
    return aliases


def omni_view_name_for_table(view_names: dict[str, Any], table: str) -> str | None:
    exact = f"main__{table}"
    names = [str(name) for name in view_names.values() if name]
    for name in names:
        if name == exact:
            return name
    for name in names:
        if name.endswith(f"__{table}"):
            return name
    return None


def detect_field_renames(
    pr_number: int, view_names: dict[str, Any]
) -> list[tuple[str, str, str]]:
    """Pair 1:1 warehouse column renames as (omni_view, old_field, new_field)."""
    ci = ci_table_columns(pr_schema(pr_number))
    prod = ci_table_columns(prod_schema())
    renames: list[tuple[str, str, str]] = []
    for table, ci_cols in ci.items():
        prod_cols = set(prod.get(table) or [])
        if not prod_cols:
            continue
        dropped = prod_cols - set(ci_cols)
        added = set(ci_cols) - prod_cols
        if not dropped or not added:
            continue
        aliases = dbt_sql_aliases(table)
        paired: list[tuple[str, str]] = []
        remaining_dropped = set(dropped)
        remaining_added = set(added)
        for old in list(remaining_dropped):
            new = aliases.get(old)
            if new and new in remaining_added:
                paired.append((old, new))
                remaining_dropped.discard(old)
                remaining_added.discard(new)
        if len(remaining_dropped) == 1 and len(remaining_added) == 1:
            paired.append((remaining_dropped.pop(), remaining_added.pop()))
        view = omni_view_name_for_table(view_names, table)
        if not view:
            print(f"No Omni view for table {table}; skip {paired}")
            continue
        for old, new in paired:
            print(f"Detected rename {view}.{old} -> {view}.{new}")
            renames.append((view, old, new))
    return renames


def replace_field(branch_id: str, find: str, replacement: str) -> dict[str, Any]:
    payload = omni_request(
        "POST",
        f"/api/v1/models/{model_id()}/content-validator",
        {
            "branch_id": branch_id,
            "find": find,
            "replacement": replacement,
            "find_or_replace_type": "FIELD",
            "include_personal_folders": True,
        },
    )
    return payload if isinstance(payload, dict) else {}


def commit_branch_to_git(branch_id: str, commit_message: str) -> dict[str, Any]:
    payload = omni_request(
        "POST",
        f"/api/v1/models/{model_id()}/git/commit",
        {
            "branch_id": branch_id,
            "commit_message": commit_message,
            "allow_branch_exists": True,
        },
    )
    return payload if isinstance(payload, dict) else {}


def autofix_branch(pr_number: int, branch_id: str, git_branch: str) -> dict[str, Any]:
    view_names = list_view_names(branch_id)
    replacements: list[dict[str, Any]] = []
    for view, old, new in detect_field_renames(pr_number, view_names):
        find = f"{view}.{old}"
        replacement = f"{view}.{new}"
        result = replace_field(branch_id, find, replacement)
        print(f"Replaced {find} -> {replacement}: {result}")
        replacements.append(
            {
                "find": find,
                "replacement": replacement,
                "replaced_documents_count": result.get("replaced_documents_count"),
                "replaced_queries_count": result.get("replaced_queries_count"),
                "replaced_dashboard_filters_count": result.get(
                    "replaced_dashboard_filters_count"
                ),
                "skipped_pr_required_count": result.get("skipped_pr_required_count"),
            }
        )
    commit: dict[str, Any] = {}
    try:
        # Git must not record schema dbt_pr_* — merging that after cleanup
        # points production at a dropped schema.
        align_views_to_schema(prod_schema(), branch_id)
        commit = commit_branch_to_git(
            branch_id,
            f"Auto-heal Omni model for dbt PR #{pr_number} ({git_branch})",
        )
        print(f"Omni git commit: {commit}")
        pr_url = ensure_omni_model_pr(git_branch, pr_number, commit)
        if pr_url:
            commit["pr_url"] = pr_url
    except Exception as exc:
        print(f"Omni git commit failed: {exc}")
        commit = {"error": str(exc)}
    align_views_to_ci_schema(pr_number, branch_id)
    return {"replacements": replacements, "commit": commit}


def content_validator(branch_id: str | None) -> dict[str, Any]:
    query = {
        "include_personal_folders": "true",
        "content_filter_mode": "WITH_ISSUES",
    }
    if branch_id:
        query["branch_id"] = branch_id
    return omni_request(
        "GET",
        f"/api/v1/models/{model_id()}/content-validator",
        query=query,
    )


def summarize_validator(payload: dict[str, Any]) -> tuple[int, list[str]]:
    broken: list[str] = []
    for doc in payload.get("content") or []:
        name = doc.get("name") or doc.get("identifier") or doc.get("document_id")
        issues: list[str] = []
        for query in doc.get("queries_and_issues") or []:
            issues.extend(_issue_text(item) for item in (query.get("issues") or []))
        issues.extend(_issue_text(item) for item in (doc.get("dashboard_filter_issues") or []))
        issues = [issue for issue in issues if issue]
        if issues:
            broken.append(f"- **{name}**: " + "; ".join(issues[:8]))
    return len(broken), broken


def _issue_text(issue: Any) -> str:
    if isinstance(issue, str):
        return issue.replace("\n", " ").strip()
    if isinstance(issue, dict):
        nested = issue.get("issues") or issue.get("message")
        if isinstance(nested, list):
            return "; ".join(_issue_text(item) for item in nested)
        if isinstance(nested, str):
            prefix = issue.get("filter_name") or issue.get("field") or ""
            return f"{prefix}: {nested}".strip(": ")
        return json.dumps(issue)
    return str(issue)


def branch_url(branch: dict[str, Any], git_branch: str) -> str:
    branch_id = branch.get("id") or ""
    return (
        f"{base_url()}/models/{model_id()}"
        f"?branchId={urllib.parse.quote(str(branch_id))}"
        f"&branch={urllib.parse.quote(git_branch)}"
    )


def github_request(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> Any:
    auth = token or env("GITHUB_TOKEN")
    if not auth:
        raise RuntimeError("GITHUB_TOKEN is not set")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {auth}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dbt-omni-preview",
        },
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub {method} {url} -> HTTP {exc.code}: {detail}") from exc


def normalize_github_url(url: str) -> str:
    return (url or "").replace("https://github.com:443/", "https://github.com/")


def omni_git_repo() -> str:
    return env("OMNI_GIT_REPO", "abarnish1024/omni_test_env")


def github_write_token() -> str:
    return env("OMNI_GIT_TOKEN") or env("GITHUB_TOKEN")


def ensure_omni_model_pr(
    git_branch: str, pr_number: int, commit: dict[str, Any]
) -> str | None:
    raw = normalize_github_url(str(commit.get("pr_url") or ""))
    if "/pull/" in raw:
        return raw
    token = github_write_token()
    repo = omni_git_repo()
    if not token:
        return raw or None
    owner = repo.split("/")[0]
    try:
        existing = github_request(
            "GET",
            f"https://api.github.com/repos/{repo}/pulls"
            f"?head={urllib.parse.quote(owner + ':' + git_branch)}&state=open",
            token=token,
        )
        if isinstance(existing, list) and existing:
            html = existing[0].get("html_url")
            print(f"Reusing Omni git PR {html}")
            return html
        created = github_request(
            "POST",
            f"https://api.github.com/repos/{repo}/pulls",
            {
                "title": f"Omni auto-heal for dbt PR #{pr_number} ({git_branch})",
                "head": git_branch,
                "base": env("OMNI_GIT_BASE", "main"),
                "body": (
                    f"Opened by dbt preview auto-heal (`{AUTOFIX_LABEL}`).\n\n"
                    f"Merge **after** dbt PR #{pr_number} lands and prod schema refresh succeeds."
                ),
            },
            token=token,
        )
        html = created.get("html_url") if isinstance(created, dict) else None
        print(f"Opened Omni git PR {html}")
        return html or raw or None
    except Exception as exc:
        print(f"Could not open Omni git PR on {repo}: {exc}")
        return raw or None


def git_sync_shared() -> dict[str, Any]:
    payload = omni_request(
        "POST",
        f"/api/v1/models/{model_id()}/git/sync",
        {},
    )
    print(f"Omni git sync: {payload}")
    return payload if isinstance(payload, dict) else {}


def merge_omni_github_pr(git_branch: str) -> str | None:
    token = github_write_token()
    repo = omni_git_repo()
    if not token:
        print("No OMNI_GIT_TOKEN/GITHUB_TOKEN; skip merging Omni git PR")
        return None
    owner = repo.split("/")[0]
    existing = github_request(
        "GET",
        f"https://api.github.com/repos/{repo}/pulls"
        f"?head={urllib.parse.quote(owner + ':' + git_branch)}&state=open",
        token=token,
    )
    if not isinstance(existing, list) or not existing:
        print(f"No open Omni git PR for {git_branch}")
        return None
    number = existing[0].get("number")
    html = existing[0].get("html_url")
    github_request(
        "PUT",
        f"https://api.github.com/repos/{repo}/pulls/{number}/merge",
        {
            "merge_method": "merge",
            "commit_title": f"Merge Omni model updates from {git_branch}",
        },
        token=token,
    )
    print(f"Merged Omni git PR {html}")
    return html


def merge_omni_model_branch(git_branch: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(git_branch, safe="")
    payload = omni_request(
        "POST",
        f"/api/v1/models/{model_id()}/branch/{encoded}/merge",
        {
            "force_override_git_settings": True,
            "delete_branch": False,
            "commit_message": f"Promote Omni branch {git_branch} after dbt prod",
        },
    )
    print(f"Omni branch merge: {payload}")
    return payload if isinstance(payload, dict) else {}


def comment_on_pr(pr_number: int, markdown: str) -> None:
    repo = env("GITHUB_REPOSITORY")
    if not env("GITHUB_TOKEN") or not repo:
        print("Skipping GitHub comment (GITHUB_TOKEN or GITHUB_REPOSITORY not set)")
        return
    comments_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    existing = github_request("GET", comments_url + "?per_page=100")
    comment_id = None
    if isinstance(existing, list):
        for comment in existing:
            if COMMENT_MARKER in (comment.get("body") or ""):
                comment_id = comment.get("id")
                break
    body = {"body": f"{COMMENT_MARKER}\n{markdown}"}
    if comment_id:
        github_request(
            "PATCH",
            f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}",
            body,
        )
        print(f"Updated GitHub PR comment {comment_id}")
        return
    github_request("POST", comments_url, body)
    print(f"Posted GitHub PR comment on #{pr_number}")


def preview(pr_number: int, git_branch: str, autofix: bool = False) -> None:
    dbt_env = create_or_update_dbt_env(pr_number)
    dbt_env_id = dbt_env.get("id")
    if not dbt_env_id:
        raise RuntimeError(f"dbt environment response missing id: {dbt_env}")
    branch = create_or_get_branch(git_branch)
    branch_id = branch.get("id")
    if not branch_id:
        raise RuntimeError(f"Omni branch response missing id: {branch}")
    set_branch_dbt(git_branch, dbt_env_id)
    dbt_sync(branch_id)
    refresh_schema(branch_id)
    align_views_to_ci_schema(pr_number, branch_id)
    validator = content_validator(branch_id)
    broken_count, lines = summarize_validator(validator)
    url = branch_url(branch, git_branch)
    schema = pr_schema(pr_number)
    status = "no broken references" if broken_count == 0 else f"{broken_count} document(s) with issues"
    issue_block = "\n".join(lines[:40]) if lines else "_None_"

    heal_block = (
        f"_Add the `{AUTOFIX_LABEL}` label to find/replace 1:1 renamed fields "
        "on this Omni branch and open the Omni git PR._"
    )
    if autofix:
        heal = autofix_branch(pr_number, branch_id, git_branch)
        replace_lines = []
        for item in heal.get("replacements") or []:
            docs = item.get("replaced_documents_count")
            queries = item.get("replaced_queries_count")
            skipped = item.get("skipped_pr_required_count")
            extra = f" ({docs} doc(s), {queries} quer(ies)"
            if skipped:
                extra += f", {skipped} skipped (PR required)"
            extra += ")"
            replace_lines.append(
                f"- `{item.get('find')}` → `{item.get('replacement')}`{extra}"
            )
        if not replace_lines:
            replace_lines.append("- _No 1:1 column renames detected vs prod schema_")
        commit = heal.get("commit") or {}
        pr_url = commit.get("pr_url")
        if commit.get("error"):
            git_line = f"- **Omni git PR:** failed — `{commit['error']}`"
        elif pr_url:
            git_line = f"- **Omni git PR:** {pr_url}"
        else:
            git_line = "- **Omni git PR:** opened or updated (no URL returned)"
        after = content_validator(branch_id)
        after_count, after_lines = summarize_validator(after)
        after_status = (
            "no broken references"
            if after_count == 0
            else f"{after_count} document(s) with issues"
        )
        after_block = "\n".join(after_lines[:40]) if after_lines else "_None_"
        heal_block = f"""**Auto-heal (`{AUTOFIX_LABEL}`)**

{chr(10).join(replace_lines)}
{git_line}

**Content validator after heal:** {after_status}

{after_block}"""

    markdown = f"""## Omni preview for dbt PR #{pr_number}

Open this Omni branch to validate downstream BI impact before merge:

- **Omni branch:** `{git_branch}`
- **dbt environment:** `{env_name(pr_number)}` → schema `{schema}` / database `{md_database()}`
- **Open in Omni:** {url}
- **Content validator:** {status}

{issue_block}

{heal_block}

### Tandem PRs

1. Merge **this dbt PR first**, wait for prod `dbt build` + schema refresh.
2. Merge the Omni PR on `omni_test_env` (same branch name `{git_branch}`).
"""
    print(markdown)
    comment_on_pr(pr_number, markdown)


def drop_motherduck_schema(schema: str) -> None:
    token = env("MOTHERDUCK_TOKEN")
    if not token:
        print("MOTHERDUCK_TOKEN not set; skip dropping warehouse schema")
        return
    try:
        import duckdb
    except ImportError as exc:
        raise SystemExit("duckdb is required to drop CI schemas") from exc
    con = duckdb.connect(f"md:{md_database()}?motherduck_token={token}")
    con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    print(f"Dropped MotherDuck schema {schema}")


def cleanup(pr_number: int, git_branch: str) -> None:
    branch = find_branch(git_branch)
    if branch and branch.get("id"):
        print(f"Retargeting Omni branch {git_branch} to {prod_schema()} before dropping CI schema")
        align_views_to_schema(prod_schema(), branch["id"])
        try:
            refresh_schema(branch["id"])
        except Exception as exc:
            print(f"Schema refresh after retarget failed: {exc}")
    else:
        print(f"No Omni branch named {git_branch}; skip retarget")
    schema = pr_schema(pr_number)
    drop_motherduck_schema(schema)
    name = env_name(pr_number)
    existing = next((item for item in list_dbt_environments() if item.get("name") == name), None)
    if existing:
        omni_request(
            "DELETE",
            f"/api/v1/connections/{connection_id()}/dbt/environments/{existing['id']}",
        )
        print(f"Deleted Omni dbt env {name}")
    else:
        print(f"No Omni dbt env named {name}")
    print(
        f"Left Omni model branch `{git_branch}` in place. Prod refresh merges the "
        "omni_test_env PR and git-syncs the shared model."
    )


def refresh_prod(git_branch: str = "") -> None:
    branch_id = None
    if git_branch:
        branch = find_branch(git_branch)
        if not branch or not branch.get("id"):
            raise RuntimeError(f"No Omni branch named {git_branch} to refresh")
        branch_id = branch["id"]
        align_views_to_schema(prod_schema(), branch_id)
        refresh_schema(branch_id)
        try:
            commit = commit_branch_to_git(
                branch_id,
                f"Point Omni views at {prod_schema()} after prod dbt build",
            )
            print(f"Omni git commit: {commit}")
            ensure_omni_model_pr(git_branch, 0, commit)
        except Exception as exc:
            print(f"Omni git commit after prod refresh failed: {exc}")
        try:
            merge_omni_github_pr(git_branch)
        except Exception as exc:
            print(f"Merging Omni git PR failed: {exc}")
        try:
            git_sync_shared()
        except Exception as exc:
            print(f"Omni git sync failed: {exc}")
        try:
            merge_omni_model_branch(git_branch)
        except Exception as exc:
            print(f"Omni branch merge failed: {exc}")
        try:
            git_sync_shared()
        except Exception as exc:
            print(f"Omni git sync after promote failed: {exc}")
        print("Production schema refresh completed")
        return
    refresh_schema(branch_id)
    print("Production schema refresh completed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Omni preview CI for dbt PRs")
    sub = parser.add_subparsers(dest="command", required=True)
    preview_cmd = sub.add_parser("preview", help="Create Omni preview env for a dbt PR")
    preview_cmd.add_argument("--pr-number", type=int, required=True)
    preview_cmd.add_argument("--git-branch", required=True)
    preview_cmd.add_argument(
        "--autofix",
        action="store_true",
        help=f"Find/replace 1:1 field renames and open the Omni git PR ({AUTOFIX_LABEL})",
    )
    cleanup_cmd = sub.add_parser("cleanup", help="Drop PR schema and Omni dbt env")
    cleanup_cmd.add_argument("--pr-number", type=int, required=True)
    cleanup_cmd.add_argument("--git-branch", required=True)
    refresh_cmd = sub.add_parser("refresh-prod", help="Refresh the shared Omni model after prod dbt")
    refresh_cmd.add_argument(
        "--git-branch",
        default="",
        help="Omni branch to refresh (required when branch-based schema refresh is on)",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    if args.command == "preview":
        preview(args.pr_number, args.git_branch, autofix=args.autofix)
    elif args.command == "cleanup":
        cleanup(args.pr_number, args.git_branch)
    elif args.command == "refresh-prod":
        refresh_prod(git_branch=args.git_branch)
    else:
        raise SystemExit(f"Unknown command {args.command}")


if __name__ == "__main__":
    main()
