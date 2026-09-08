#!/usr/bin/env python3
"""Create or tear down a per-PR Omni preview environment for dbt changes.

preview:
  1. Create/reuse a shared Omni dbt environment pointed at schema dbt_pr_<N>
  2. Create/reuse an Omni model branch named after the git branch
  3. Point that branch at the dbt env + dbt git branch
  4. Schema-refresh the Omni branch
  5. Run the content validator
  6. Comment on the GitHub PR (when GITHUB_TOKEN is set)

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
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMMENT_MARKER = "<!-- omni-preview-ci -->"


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
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Omni {method} {path} -> HTTP {exc.code}: {detail}") from exc


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


def poll_job(job_id: str, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while True:
        payload = omni_request("GET", f"/api/v1/jobs/{job_id}/status")
        status = (payload.get("status") or "").upper()
        print(f"Schema refresh job {job_id}: {status or payload}")
        if status in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
            return
        if status in {"FAILED", "ERROR", "CANCELLED"}:
            raise RuntimeError(f"Schema refresh failed: {payload}")
        if time.time() > deadline:
            raise RuntimeError(f"Timed out waiting for schema refresh {job_id}")
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
    poll_job(job_id)


def content_validator(branch_id: str | None) -> dict[str, Any]:
    query = {}
    if branch_id:
        query["branch_id"] = branch_id
    return omni_request(
        "GET",
        f"/api/v1/models/{model_id()}/content-validator",
        query=query or None,
    )


def summarize_validator(payload: dict[str, Any]) -> tuple[int, list[str]]:
    broken: list[str] = []
    for doc in payload.get("content") or []:
        name = doc.get("name") or doc.get("identifier") or doc.get("document_id")
        issues: list[str] = []
        for query in doc.get("queries_and_issues") or []:
            issues.extend(query.get("issues") or [])
        issues.extend(doc.get("dashboard_filter_issues") or [])
        issues = [issue for issue in issues if issue]
        if issues:
            broken.append(f"- **{name}**: " + "; ".join(issues[:8]))
    return len(broken), broken


def branch_url(branch: dict[str, Any], git_branch: str) -> str:
    branch_id = branch.get("id") or ""
    return (
        f"{base_url()}/models/{model_id()}"
        f"?branchId={urllib.parse.quote(str(branch_id))}"
        f"&branch={urllib.parse.quote(git_branch)}"
    )


def github_request(method: str, url: str, body: dict[str, Any] | None = None) -> Any:
    token = env("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dbt-omni-preview",
        },
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


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


def preview(pr_number: int, git_branch: str) -> None:
    dbt_env = create_or_update_dbt_env(pr_number)
    dbt_env_id = dbt_env.get("id")
    if not dbt_env_id:
        raise RuntimeError(f"dbt environment response missing id: {dbt_env}")
    branch = create_or_get_branch(git_branch)
    branch_id = branch.get("id")
    if not branch_id:
        raise RuntimeError(f"Omni branch response missing id: {branch}")
    set_branch_dbt(git_branch, dbt_env_id)
    refresh_schema(branch_id)
    validator = content_validator(branch_id)
    broken_count, lines = summarize_validator(validator)
    url = branch_url(branch, git_branch)
    schema = pr_schema(pr_number)
    status = "no broken references" if broken_count == 0 else f"{broken_count} document(s) with issues"
    issue_block = "\n".join(lines[:40]) if lines else "_None_"
    markdown = f"""## Omni preview for dbt PR #{pr_number}

Open this Omni branch to validate downstream BI impact before merge:

- **Omni branch:** `{git_branch}`
- **dbt environment:** `{env_name(pr_number)}` → schema `{schema}` / database `{md_database()}`
- **Open in Omni:** {url}
- **Content validator:** {status}

{issue_block}

### Tandem PRs

1. Log into Omni, switch to branch `{git_branch}`, and inspect dashboards.
2. Fix broken field references on the Omni branch (Content Validator find/replace).
3. Omni opens/syncs a PR on `omni_test_env` with the same branch name.
4. Merge **this dbt PR first**, wait for prod `dbt build` + schema refresh, then merge the Omni PR.
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
        f"Left Omni model branch `{git_branch}` in place so YAML/content PRs on "
        "omni_test_env can still be merged."
    )


def refresh_prod() -> None:
    refresh_schema(None)
    print("Production schema refresh completed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Omni preview CI for dbt PRs")
    sub = parser.add_subparsers(dest="command", required=True)
    preview_cmd = sub.add_parser("preview", help="Create Omni preview env for a dbt PR")
    preview_cmd.add_argument("--pr-number", type=int, required=True)
    preview_cmd.add_argument("--git-branch", required=True)
    cleanup_cmd = sub.add_parser("cleanup", help="Drop PR schema and Omni dbt env")
    cleanup_cmd.add_argument("--pr-number", type=int, required=True)
    cleanup_cmd.add_argument("--git-branch", required=True)
    sub.add_parser("refresh-prod", help="Refresh the shared Omni model after prod dbt")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    if args.command == "preview":
        preview(args.pr_number, args.git_branch)
    elif args.command == "cleanup":
        cleanup(args.pr_number, args.git_branch)
    elif args.command == "refresh-prod":
        refresh_prod()
    else:
        raise SystemExit(f"Unknown command {args.command}")


if __name__ == "__main__":
    main()
