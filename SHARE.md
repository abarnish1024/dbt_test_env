# dbt PR preview + Omni blast radius

Reference implementation:

- dbt repo: [https://github.com/abarnish1024/dbt_test_env](https://github.com/abarnish1024/dbt_test_env)
- Omni model git (example only — do not reuse the YAML): [https://github.com/abarnish1024/omni_test_env](https://github.com/abarnish1024/omni_test_env)
- Omni guide: [https://docs.omni.co/guides/modeling/preview-dbt-changes](https://docs.omni.co/guides/modeling/preview-dbt-changes)

## What you get

1. Open a dbt PR.
2. GitHub Actions builds into a temporary warehouse schema (`dbt_pr_<PR>`).
3. CI creates a matching Omni dbt environment (`ci-pr-<PR>`) and an Omni **model branch** named after the git branch.
4. CI rewrites Omni views onto that schema. Omni’s dbt-sync still compiles `main`, so a schema refresh alone will not pick up renamed columns.
5. Content Validator comments on the dbt PR with broken dashboards.
6. Optional `omni-autofix` label: 1:1 column renames are find/replaced on the Omni branch, and a PR is opened on the **Omni model git** repo.
7. Merge **dbt first**. Prod `dbt build` writes the new columns, then CI merges the Omni git PR and `git/sync`s the shared model so dashboards do not stay dark.

Use the **same branch name** in both repos.

## What to copy

Portable files from this repo:


| Path                                 | Why                                                |
| ------------------------------------ | -------------------------------------------------- |
| `scripts/omni_preview.py`            | Preview, autofix, cleanup, prod refresh            |
| `.github/workflows/omni-preview.yml` | PR opened/updated/labeled                          |
| `.github/workflows/omni-cleanup.yml` | PR closed: drop CI schema + Omni dbt env           |
| `.github/workflows/dbt-prod.yml`     | `main` push: prod build + Omni promote             |
| `macros/generate_schema_name.sql`    | CI writes `DBT_SCHEMA`; prod stays the real schema |
| `requirements.txt`                   | Swap the adapter if they are not DuckDB            |


Do **not** copy:

- `.env` / secrets
- This instance’s Omni URL, connection ID, or model ID
- The Omni model git contents (`omni_test_env`) — they connect Omni git to **their** model repo
- The F1 demo marts, unless they only want a sandbox

They also need a **second GitHub repo** for the Omni shared model (empty or Omni-initialized). Omni writes `.view` / `.topic` YAML there.

Create an `omni-autofix` label on the dbt repo if they want auto-heal.

## What they change for their Omni instance



### 1. GitHub Actions variables (on the dbt repo)

Set Actions **variables**, or replace the defaults in the three workflow files:


| Variable             | Set to                                               |
| -------------------- | ---------------------------------------------------- |
| `OMNI_BASE_URL`      | `https://<their-org>.omniapp.co`                     |
| `OMNI_CONNECTION_ID` | Warehouse connection UUID                            |
| `OMNI_MODEL_ID`      | **Shared** model UUID (not a branch, not a workbook) |


In Omni: connection settings → connection ID. Open the shared model → the UUID is in the URL / model settings.

Optional env (script defaults; set in workflows if they differ):


| Env                   | Default in this repo         | They set                                   |
| --------------------- | ---------------------------- | ------------------------------------------ |
| `OMNI_GIT_REPO`       | `abarnish1024/omni_test_env` | `org/their-omni-model-repo`                |
| `OMNI_GIT_BASE`       | `main`                       | Omni git default branch                    |
| `OMNI_PROD_SCHEMA`    | `main`                       | Their prod schema (`prod`, `analytics`, …) |
| `MOTHERDUCK_DATABASE` | `my_db`                      | Their database name, or drop MotherDuck    |


Add `OMNI_GIT_REPO` to the workflow `env:` blocks if they are not using the script default.

### 2. GitHub secrets (on the **dbt** repo)


| Secret                | What it is                                                                                 |
| --------------------- | ------------------------------------------------------------------------------------------ |
| `OMNI_API_KEY`        | Omni org API key (`omni_osk_…`). **Not** a GitHub token.                                   |
| Warehouse credentials | This demo uses `MOTHERDUCK_TOKEN`. Snowflake / BigQuery / etc. instead.                    |
| `OMNI_GIT_TOKEN`      | GitHub PAT (`ghp_…`) with `repo` on the **Omni model** repo, so prod CI can merge that PR. |


`GITHUB_TOKEN` is automatic for PR comments. Allow Actions to comment on PRs if the org blocks that.

### 3. One-time Omni UI (CI cannot do this)

On **their** connection + shared model, as Connection Admin / Organization Admin:

1. Connect **dbt GitHub** to **their dbt repo**.
2. Create a **production dbt environment** (their prod database + schema).
3. Turn on **Virtual Schemas** so a branch can switch dbt environments.
4. Connect **Omni git** to **their** Omni model GitHub repo. Turn **Require pull requests** on. Turn **branch-based schema refresh** on.
5. On that Omni model repo, add Omni’s pull-request webhook (Pull requests events, content-type JSON). Omni Git Settings shows the URL:
  `https://<org>.omniapp.co/webhooks/model/<model-id>/pull_request`

Until those clicks exist, `omni_preview.py` fails with `dbt not configured` / `Git not configured`.

### 4. Warehouse / dbt adapter

This demo is **dbt-duckdb + MotherDuck**. `omni_preview.py` inspects columns with the `duckdb` Python driver (`ci_table_columns()`).

If they use Snowflake, BigQuery, Redshift, Databricks, etc.:

- Change `profiles.yml` and the dbt adapter in `requirements.txt`.
- Change `ci_table_columns()` to query that warehouse’s `information_schema` (or equivalent).
- Replace `MOTHERDUCK_*` workflow env with their credentials.
- Keep the pattern: CI target writes `dbt_pr_<N>`; prod target writes the real schema.

`macros/generate_schema_name.sql` already honors `target.name == 'ci'` so CI does not prefix `dbt_pr_*` with the prod schema name.

## How they operate it

1. Feature branch. Never commit column renames straight to `main`.
2. Open the dbt PR. Wait for the Omni preview comment (branch URL + validator).
3. Optional: add the `omni-autofix` label. Ignore **git out of sync** on the preview branch. Do not open a second PR from the Omni git UI.
4. Merge the **dbt** PR. Prod CI builds the warehouse, merges the Omni git PR, and syncs the shared model.
5. In Omni, analyze on the **shared model**, not a leftover feature branch.

Do not merge the Omni git PR before prod has the new columns. CI does that after prod `dbt build`.

## Gotchas

- `OMNI_GIT_TOKEN` must be a GitHub PAT (`ghp_…`), not an Omni API key (`omni_osk_…`).
- After autofix, the live Omni branch may still point at `dbt_pr_*` until prod refresh. “Git out of sync” on that branch is expected.
- 1:1 column renames auto-heal. Grain or logic changes do not.
- Omni branch names with slashes (`feat/foo`) must be URL-encoded on the merge API; the script does that.
- If dashboards still query the old field after merge, you are probably on a leftover Omni feature branch — switch to the shared model.
- Prod refresh needs the Omni **branch** that preview created. Always open dbt changes as a PR from a feature branch, not a commit on `main`.



## Minimal checklist for them

- [ ] Copy the files above into their dbt repo
- [ ] Create / connect Omni model git repo
- [ ] Set `OMNI_BASE_URL`, `OMNI_CONNECTION_ID`, `OMNI_MODEL_ID`, `OMNI_GIT_REPO`
- [ ] Set secrets: `OMNI_API_KEY`, warehouse token, `OMNI_GIT_TOKEN`
- [ ] Omni UI: dbt GitHub, prod dbt env, Virtual Schemas, Omni git + require PRs, branch-based refresh, PR webhook
- [ ] Create `omni-autofix` label
- [ ] Open a feature PR that renames one mart column and confirm the preview comment