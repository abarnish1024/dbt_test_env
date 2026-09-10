# dbt PR preview + Omni blast radius

Reference implementation:

- dbt repo: [https://github.com/abarnish1024/dbt_test_env](https://github.com/abarnish1024/dbt_test_env)
- Omni model git (example only — do not reuse the YAML): [https://github.com/abarnish1024/omni_test_env](https://github.com/abarnish1024/omni_test_env)
- Omni guide: [https://docs.omni.co/guides/modeling/preview-dbt-changes](https://docs.omni.co/guides/modeling/preview-dbt-changes)

## What you get

1. Open a dbt PR.
2. GitHub Actions builds into a temporary warehouse schema (`dbt_pr_<PR>`).
3. CI creates Omni dbt env `ci-pr-<PR>` and a **new Omni model branch** `pr-<N>` (not the dbt git branch name, and not reused across PRs).
4. Hard-refresh first so Omni sees `dbt_pr_<N>`, then dbt-sync. Physical `main__` views are rewritten onto `dbt_pr_<N>` for preview only — dashboards use those views, not `omni_dbt__`.
5. Content Validator comments on the dbt PR with broken dashboards.
6. Optional `omni-autofix`: find/replace 1:1 renames, rewrite views back to `schema: main`, production dbt env, hard-refresh, Omni `git/commit` on git branch `pr-<N>`.
7. Merge **dbt first**. Prod `dbt build`, then: views on `main` → prod env → hard refresh → commit → merge Omni git PR → merge Omni branch (`delete_branch: true`) → drop `ci-pr-<N>`. Do not call `/git/sync`.

## What to copy

Portable files from this repo:


| Path                                 | Why                                                |
| ------------------------------------ | -------------------------------------------------- |
| `scripts/omni_preview.py`            | Preview, autofix, cleanup, prod refresh            |
| `.github/workflows/omni-preview.yml` | PR opened/updated/labeled                          |
| `.github/workflows/omni-cleanup.yml` | Abandoned PR only: drop CI schema + Omni dbt env |
| `.github/workflows/dbt-prod.yml`     | `main` push: prod build, Omni promote, then cleanup |
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
| `OMNI_PROD_DBT_ENV_NAME` | `Production`              | Name of the default Omni dbt environment |
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
3. Optional: add the `omni-autofix` label. Never commit `schema: dbt_pr_N` or while the Omni branch is on `ci-pr-<N>`. Do not open a second PR from the Omni git UI.
4. Merge the **dbt** PR. Prod CI rewrites views to `main`, points `pr-<N>` at prod, commits, merges the Omni git PR, promotes and deletes that Omni branch, then drops the CI env.
5. In Omni, analyze on the **shared model**, not a leftover feature branch.

Do not merge the Omni git PR before prod has the new columns. CI does that after prod `dbt build`. Do not call `POST /git/sync` when require-PRs is on — that only sets the out-of-sync banner.

## Gotchas

- `OMNI_GIT_TOKEN` must be a GitHub PAT (`ghp_…`), not an Omni API key (`omni_osk_…`).
- Never Omni `git/commit` while a physical view has `schema: dbt_pr_N`, and never commit while the branch’s dbt environment is `ci-pr-<N>`. Both make CI schema a real active schema that rides into shared after cleanup.
- Preview order is **hard-refresh, then dbt-sync**. Sync first matches against a schema model that has not seen `dbt_pr_N` yet and the env looks empty.
- Omni model branch is `pr-<N>`, deleted on promote. Reusing `feat/foo` across dbt PRs flip-flops git.
- Do not call `/git/sync` on this path. With require-PRs, it cannot write `main` and only flags the banner. If the banner shows anyway: Update PR from any branch, merge it, and the next git action reads in sync.
- Cleanup must run **after** refresh-prod on merge.
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