# dbt Core + GitHub + Omni preview CI

This repo is the **dbt Core** side of a tandem GitHub + Omni workflow:

1. Open a dbt PR.
2. CI builds models into a temporary MotherDuck schema (`dbt_pr_<PR>`).
3. CI creates a matching Omni dbt environment and Omni model **branch**.
4. You log into Omni, open that branch, and use Content Validator to see downstream BI impact.
5. Omni YAML/content fixes go out as a **second PR** on [`abarnish1024/omni_test_env`](https://github.com/abarnish1024/omni_test_env) with the **same branch name**.
6. Merge dbt first, then Omni, so dashboards never go dark.

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
python scripts/omni_preview.py preview --pr-number 0 --git-branch "$(git branch --show-current)"
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

## Tandem merge order

Use the **same branch name** in both repos (for example `feat/rename-points`).

1. Open the dbt PR. Wait for the Omni preview comment.
2. Open the Omni branch from that comment. Run Content Validator. Fix references / model YAML.
3. Omni syncs a PR on `omni_test_env`.
4. **Merge the dbt PR.** `dbt-prod.yml` runs `dbt build --target prod` into `main` and refreshes the shared Omni model.
5. **Merge the Omni PR** so dashboards pick up the new field names.

Do not merge the Omni PR before the warehouse and schema refresh have the new columns.

## Cleanup

Closing or merging the dbt PR drops `dbt_pr_<N>` in MotherDuck and deletes Omni dbt env `ci-pr-<N>`. The Omni **model branch** is left in place so the Omni git PR can still merge.
