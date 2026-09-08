# dbt Core + GitHub + Omni preview CI

This repo is the **dbt Core** side of a tandem GitHub + Omni workflow:

1. Open a dbt PR.
2. CI builds models into a temporary MotherDuck schema (`dbt_pr_<PR>`).
3. CI creates a matching Omni dbt environment and Omni model **branch**.
4. CI rewrites the Omni branch views onto `dbt_pr_<PR>` (Omni's dbt-sync still compiles against `main`, so a schema refresh alone does not rename columns).
5. Content Validator runs. The PR comment lists broken dashboards.
6. Optional: add the **`omni-autofix`** label. CI find/replaces 1:1 renamed fields on the Omni branch and opens/updates a PR on [`omni_test_env`](https://github.com/abarnish1024/omni_test_env).
7. Merge dbt first, then Omni, so dashboards never go dark.

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
| `.github/workflows/omni-cleanup.yml` | PR closed: drop schema + Omni dbt env |
| `.github/workflows/dbt-prod.yml` | `main` push: prod build + Omni schema refresh |

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
| `OMNI_GIT_TOKEN` | Optional GitHub PAT that can open PRs on `omni_test_env`. Needed for auto-heal: Omni can push the branch (deploy key) but GitHub Actions `GITHUB_TOKEN` cannot create PRs in the other repo. |

**Variables** (optional; defaults are already in the workflows)

| Name | Default |
| --- | --- |
| `OMNI_BASE_URL` | `https://andrewbarnish.omniapp.co` |
| `OMNI_CONNECTION_ID` | `51e3303e-1e8a-4752-82f9-419eed9b4cba` |
| `OMNI_MODEL_ID` | `aa9824df-0ccd-4a92-8954-d56203363e48` |

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
4. Commits the Omni model branch to git and opens/updates the `omni_test_env` PR.

Grain or logic changes that are not a 1:1 rename are left for a human. Removing the label does not undo replacements.

## Tandem merge order

Use the **same branch name** in both repos (for example `feat/rename-points`).

1. Open the dbt PR. Wait for the Omni preview comment.
2. To see blast radius, inspect the Omni branch / validator list. To auto-fix 1:1 renames, add `omni-autofix`.
3. **Merge the dbt PR.** Prod `dbt build` writes `race_points` into `main`. Cleanup retargets the Omni branch off `dbt_pr_<N>` **before** dropping that schema.
4. **Then merge the Omni PR** on `omni_test_env`. That PR should point at schema `main`, not `dbt_pr_*`.

Do not merge the Omni PR before the warehouse and schema refresh have the new columns.

## Cleanup

Closing or merging the dbt PR retargets Omni views to `main`, then drops `dbt_pr_<N>` and deletes Omni dbt env `ci-pr-<N>`. The Omni **model branch** is left in place so the Omni git PR can still merge.
