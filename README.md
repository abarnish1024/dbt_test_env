# dbt Core + GitHub + Omni preview CI

This repo is the **dbt Core** side of a tandem GitHub + Omni workflow:

1. Open a dbt PR.
2. CI builds models into a temporary MotherDuck schema (`dbt_pr_<PR>`).
3. CI creates Omni dbt env `ci-pr-<N>` and a **new Omni model branch** `pr-<N>` (not reused across PRs).
4. Hard-refresh (so Omni sees `dbt_pr_<N>`), then dbt-sync. Physical `main__` views are rewritten onto `dbt_pr_<N>` for preview only.
5. Content Validator comments on the dbt PR with broken dashboards.
6. Optional `omni-autofix`: find/replace 1:1 renames, rewrite views back to `schema: main`, point at the production dbt env, hard-refresh, Omni `git/commit` to git branch `pr-<N>`.
7. Merge the dbt PR. Prod CI: rewrite off `dbt_pr_N`, prod env, hard-refresh, commit, merge Omni git PR, promote Omni branch (`delete_branch: true`), then drop `ci-pr-<N>`. Never `/git/sync`.

Warehouse: MotherDuck `my_db` via Omni connection **Barnish Duck**.

## Layout

| Path | Purpose |
| --- | --- |
| `models/staging` | `stg_races`, `stg_drivers`, `stg_results` from `main` F1 tables |
| `models/marts` | `fct_race_results`, `dim_drivers` |
| `macros/generate_schema_name.sql` | CI writes to `DBT_SCHEMA`; prod stays `main` |
| `profiles.yml` | dbt-duckdb / MotherDuck, secrets from env |
| `scripts/omni_preview.py` | Create/teardown Omni preview env |
| `.github/workflows/omni-preview.yml` | PR opened/updated |
| `.github/workflows/omni-cleanup.yml` | Abandoned PR: drop schema + Omni dbt env |
| `.github/workflows/dbt-prod.yml` | `main` push: prod build, Omni promote, then cleanup |

Demo blast-radius field: `fct_race_results.points`. Rename it in a feature PR to see Omni Content Validator report broken dashboards.

## Local setup

```bash
source .venv/bin/activate
set -a && source .env && set +a
export DBT_PROFILES_DIR=.
dbt build --target prod --profiles-dir .
```

CI-style local preview (does not comment on GitHub):

```bash
export DBT_SCHEMA=dbt_pr_local
dbt build --target ci --profiles-dir .
python scripts/omni_preview.py preview --pr-number 0 --git-branch "$(git branch --show-current)" --autofix
```

Requires Python 3.12+, `dbt-core`, and `dbt-duckdb` (see `requirements.txt`).

## Second repo: Omni model git

Omni YAML lives in a sibling folder, [`omni_test_env`](https://github.com/abarnish1024/omni_test_env). If that GitHub repo does not exist yet (needs `gh auth login` once):

```bash
cd ../omni_test_env
gh auth login
gh repo create abarnish1024/omni_test_env --public --source=. --remote=origin --push
```

Then complete the Omni UI checklist below so Barnish Duck git points at it.

## GitHub secrets and variables

On **this** repo (`dbt_test_env`) → Settings → Secrets and variables → Actions:

**Secrets**

| Name | Value |
| --- | --- |
| `OMNI_API_KEY` | Organization API key from Omni (Settings → API Keys) |
| `MOTHERDUCK_TOKEN` | MotherDuck read/write PAT |
| `OMNI_GIT_TOKEN` | GitHub PAT that can open **and merge** PRs on `omni_test_env`. Required so prod CI can merge the Omni git PR. |

**Variables** (optional; defaults are already in the workflows)

| Name | Default |
| --- | --- |
| `OMNI_BASE_URL` | `https://andrewbarnish.omniapp.co` |
| `OMNI_CONNECTION_ID` | `51e3303e-1e8a-4752-82f9-419eed9b4cba` |
| `OMNI_MODEL_ID` | `aa9824df-0ccd-4a92-8954-d56203363e48` |
| `OMNI_PROD_DBT_ENV_NAME` | `Production` (script default; set if theirs differs) |

The preview workflow uses `GITHUB_TOKEN` automatically to comment on the PR. Allow Actions to write pull-request comments if your org restricts that.

## Omni UI checklist (required once)

CI cannot attach GitHub to Omni. Do this in [andrewbarnish.omniapp.co](https://andrewbarnish.omniapp.co) as Connection Admin / Organization Admin:

1. **Connect dbt GitHub** on Barnish Duck to `abarnish1024/dbt_test_env`.  
   Model → connection dbt settings, or [dbt GitHub setup](https://docs.omni.co/integrations/dbt/setup).
2. **Create a production dbt environment** targeting database `my_db`, schema `main`.
3. **Enable Virtual Schemas** so a branch can switch dbt environments.
4. **Connect Omni git** on the Barnish Duck shared model to [`abarnish1024/omni_test_env`](https://github.com/abarnish1024/omni_test_env).  
   Model → Git Settings. Turn **Require pull requests** on.
5. **Enable branch-based schema refresh** so CI can call  
   `POST /api/v1/models/{id}/refresh?branch_id=…`.

Until those clicks are done, `scripts/omni_preview.py` will fail with `dbt not configured` / `Git not configured`.

## Auto-heal (`omni-autofix`)

Without the label, preview is **report-only**: the Omni branch shows the blast radius (broken `points` refs, etc.).

Add the **`omni-autofix`** label to the dbt PR (or include it when you open the PR). That re-runs preview and:

1. Diffs CI schema columns against prod `main`.
2. Treats a 1:1 drop/add (or `old as new` in the dbt SQL) as a field rename.
3. Find/replaces those fields on the Omni branch, including personal-folder dashboards.
4. Rewrites physical `main__` views back to `schema: main` (never commit `schema: dbt_pr_N`), points the Omni branch `pr-<N>` at the **production** dbt environment, hard-refreshes, then Omni `git/commit`s.
5. Opens/updates the `omni_test_env` PR on git branch `pr-<N>`. The live preview branch is then pointed back at `ci-pr-<N>` and rewritten onto the CI schema again.

Do not open a second PR from the Omni git UI. If the IDE warns that the dbt environment is not the default, you are not in a state that should be committed.

Grain or logic changes that are not a 1:1 rename are left for a human. Removing the label does not undo replacements.

## Tandem merge order

Use Omni branch `pr-<N>` (one per dbt PR). Do not reuse `feat/foo` as the Omni model branch across PRs.

1. Open the dbt PR. Wait for the Omni preview comment.
2. To see blast radius, inspect Omni branch `pr-<N>` / validator list. To auto-fix 1:1 renames, add `omni-autofix`.
3. **Merge the dbt PR.** Prod `dbt build` writes the new columns, rewrites views off `dbt_pr_N`, points `pr-<N>` at the production dbt env, hard-refreshes, Omni `git/commit`s, merges the `omni_test_env` PR, promotes and **deletes** the Omni branch, then drops `ci-pr-<N>`.
4. You should not merge anything in the Omni git UI or call `git/sync`. In Omni, use the **shared model**.

Do not merge the Omni git PR before the warehouse has the new columns. CI does that after prod `dbt build`.

## Cleanup

Abandoned (unmerged) dbt PRs drop `dbt_pr_<N>` and delete Omni dbt env `ci-pr-<N>` immediately. Merged PRs wait until **after** prod refresh so cleanup does not delete the dbt env the Omni branch is still using.
