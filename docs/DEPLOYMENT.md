# Deployment (GCP / Cloud Run)

Status: a demo/staging-style deployment, not a production one — see [README.md](../README.md)'s
"Not production ready" and §4 below before pointing real customer traffic at this.

## 1. What's deployed

| Resource | Name | Notes |
|---|---|---|
| GCP project | `hermes-rpt-demo` | Fresh project, own billing link, own IAM — isolated from other projects on the account. |
| Cloud Run service | `hermes-rpt-api` (region `us-central1`) | Runs `apps/api` (the FastAPI control-plane service). Two containers per instance: `api` and a `cloud-sql-proxy` sidecar. |
| Cloud SQL | `hermes-rpt-control-db` (Postgres 16, `db-g1-small`) | Control-plane database only — same "two planes" invariant as local dev (docs/ARCHITECTURE.md). No customer/tenant data ever lives here. |
| Artifact Registry | `hermes-rpt` (Docker, `us-central1`) | Holds `api` images. |
| Secret Manager | `hermes-rpt-jwt-secret`, `hermes-rpt-db-password`, `hermes-rpt-database-url` | See §3. |
| Service accounts | `hermes-rpt-api-runtime`, `github-deployer` | Runtime identity vs. CI/deploy identity — kept separate, least privilege each (§2). |
| Workload Identity Pool | `github-pool` / provider `github-provider` | Lets GitHub Actions authenticate to GCP with no stored key (§2). |

Not deployed (out of scope for this pass — see §4): `apps/worker`, MLflow, the synthetic Alpha/Beta
tenant databases from `docker-compose.yml`.

## 2. Identity and access

Two separate service accounts, each scoped to only what it needs:

* **`hermes-rpt-api-runtime@hermes-rpt-demo.iam.gserviceaccount.com`** — what the deployed
  container runs as. Roles: `roles/cloudsql.client`, `roles/secretmanager.secretAccessor` (on
  the three secrets above only, not project-wide).
* **`github-deployer@hermes-rpt-demo.iam.gserviceaccount.com`** — what GitHub Actions
  impersonates to deploy. Roles: `roles/run.admin`, `roles/artifactregistry.writer`,
  `roles/cloudsql.client` (to run migrations), `roles/iam.serviceAccountUser` on the runtime SA
  specifically (needed to deploy a revision that runs as it), and `secretAccessor` on
  `hermes-rpt-db-password` (to build the migration DSN).

**No service account key is stored anywhere.** GitHub Actions authenticates via [Workload
Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation): the
workflow's own GitHub-issued OIDC token is exchanged for short-lived GCP credentials, scoped by
an attribute condition to this exact repository —

```text
assertion.repository == 'sathsaraWj/Logistic-RPT'
```

— so no other repository, fork, or workflow can impersonate `github-deployer`, even if it knew
the pool/provider names (which aren't secret; they're plain resource names, not credentials).
This means **`.github/workflows/deploy.yml` needs zero GitHub repository secrets** to function —
nothing to add in Settings → Secrets.

## 3. Secrets

Three Secret Manager secrets, injected into the Cloud Run revision as env vars
(`docker/cloudrun-service.yaml`), never baked into the image or committed anywhere:

* `hermes-rpt-jwt-secret` → `JWT_SECRET_KEY` — a random 48-byte token, generated once at
  bootstrap. Real secret, not the insecure dev default (`Settings._reject_insecure_jwt_secret`
  would refuse to boot with the default outside `local`/`ci` anyway — see §4 on why
  `ENVIRONMENT=local` is still set despite that).
* `hermes-rpt-db-password` → the `hermes` Cloud SQL user's password. Used directly by CI to
  build the migration DSN; not injected into the Cloud Run container on its own.
* `hermes-rpt-database-url` → `DATABASE_URL`, two versions:
  - version `1` (what's actually wired into `docker/cloudrun-service.yaml`): the TCP form
    (`postgresql+asyncpg://hermes:...@127.0.0.1:5432/hermes_control`) the `cloud-sql-proxy`
    sidecar expects.
  - version `2`/`latest`: the Unix-socket form
    (`postgresql+asyncpg://hermes:...@/hermes_control?host=/cloudsql/...`) for Cloud Run's
    *native* `--add-cloudsql-instances` connector, kept for reference / a future switch back —
    see the "Known issues" note below for why the sidecar is used instead right now.

## 4. Known, deliberate gaps — read before treating this as "production"

* **Auth**: `ENVIRONMENT=local` is set on the deployed service, on purpose — confirmed with the
  requester before deploying. This codebase's only working token issuance path is
  `hermes_rpt.auth.dev_tokens.issue_dev_token` (HS256, shared secret), which refuses to run
  outside `local`/`ci` (`DevTokenIssuanceNotAllowedError`). There is no real OIDC/login flow
  built yet ([ADR 0010](adr/0010-jwt-local-dev-provider.md)). Issue a token to call the API:

  ```bash
  DB_PASSWORD=$(gcloud secrets versions access latest --secret=hermes-rpt-db-password --project=hermes-rpt-demo)
  # then, with DATABASE_URL pointed at the deployed instance (e.g. via cloud-sql-proxy locally):
  uv run python -c "
  from hermes_rpt.auth.dev_tokens import issue_dev_token
  from hermes_rpt.common.settings import get_settings
  import uuid
  print(issue_dev_token(settings=get_settings(), tenant_id=uuid.UUID('...'), principal_id=uuid.UUID('...'), scopes=['tenant:read']))
  "
  ```

  A real deployment needs a real `TokenVerifier` (RS256 against an OIDC provider's JWKS) before
  `ENVIRONMENT` should ever say `staging` or `production`.
* **Secrets for tenant connections**: `hermes_rpt.secrets.provider.get_secret_provider()` still
  hardcodes `LocalDevSecretProvider` (in-memory, lost on restart) regardless of environment —
  unrelated to the three infrastructure secrets in §3, which are handled at the Cloud Run/Secret
  Manager level instead. Registering a real customer connection through this deployment would
  store that customer's secret in-process memory, gone on the next cold start. Wiring
  `GoogleSecretManagerSecretProvider` for real (currently a stub — see
  `src/hermes_rpt/secrets/provider.py`) is required before this deployment is used for anything
  beyond the control-plane/governance endpoints that don't need a tenant connection.
* **Scope**: no tenant customer databases, no MLflow, no worker are deployed — `/v1/predictions/*`
  will `503` (no `PRODUCTION` model registered against this fresh instance) and
  `/v1/connections`/`/v1/schema-discovery` will work but have nothing real to discover against.
  What *is* fully live: tenant/membership management, auth, model registry/governance endpoints,
  and the health/version endpoints.
* **Known issue — Cloud Run's native Cloud SQL connector**: `--add-cloudsql-instances` (and the
  equivalent YAML) auto-injects a managed `cloud-sql-proxy` sidecar with its own startup TCP
  probe against `127.0.0.1:5432`. In this project, that probe consistently timed out
  (`STARTUP TCP probe failed ... DEADLINE_EXCEEDED`) even though the proxy itself logged "ready"
  and was independently verified reachable (a manually-run `cloud-sql-proxy` against the same
  instance connected fine). Explicitly declaring the sidecar in `docker/cloudrun-service.yaml`
  *without* a startup probe on it (Cloud Run then just checks the process is running) resolved
  it. If this turns out to be a transient, project-specific issue rather than a systemic one,
  switching back to `--add-cloudsql-instances` + the Unix-socket DSN (already generated as secret
  version `2`) would remove a moving part.
* **Cost**: Cloud SQL (`db-g1-small`, always-on) is the dominant ongoing cost — Cloud Run scales
  to zero (`--min-instances=0`) when idle, Cloud SQL does not. Tear down with `gcloud sql
  instances delete hermes-rpt-control-db` (or the whole project — see §6) if this is only needed
  for a demo window.

## 5. CI/CD — `.github/workflows/deploy.yml`

Triggers on push to `main` (path-filtered to `src/`, `apps/`, `migrations/`, `docker/`,
`pyproject.toml`, `uv.lock`) or manual dispatch. Two jobs:

1. `test` — the same lint/format/typecheck/unit-test/bandit gate as `ci.yml`. `deploy` will not
   run if this fails.
2. `deploy` — authenticates via Workload Identity Federation (§2), builds `docker/api.Dockerfile`,
   pushes to Artifact Registry tagged with the commit SHA (and `:latest`), renders
   `docker/cloudrun-service.yaml` with the real image URI, deploys via `gcloud run services
   replace`, runs `alembic upgrade head` against Cloud SQL through a short-lived
   `cloud-sql-proxy` it downloads for the job, then curls `/health/ready` to confirm the new
   revision actually came up before declaring success.

Needs zero GitHub repository secrets (§2). It does need `permissions: id-token: write`, which is
already set in the workflow.

## 6. Manual bootstrap (already done for `hermes-rpt-demo`; reference for a fresh project)

```bash
gcloud projects create <PROJECT_ID> --name="..."
gcloud billing projects link <PROJECT_ID> --billing-account=<ACCOUNT_ID>
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
  compute.googleapis.com sts.googleapis.com cloudbuild.googleapis.com --project=<PROJECT_ID>

gcloud artifacts repositories create hermes-rpt --repository-format=docker --location=us-central1 --project=<PROJECT_ID>
gcloud sql instances create hermes-rpt-control-db --database-version=POSTGRES_16 --edition=ENTERPRISE \
  --tier=db-g1-small --region=us-central1 --storage-size=10GB --storage-auto-increase --no-backup --project=<PROJECT_ID>
gcloud sql databases create hermes_control --instance=hermes-rpt-control-db --project=<PROJECT_ID>
gcloud sql users create hermes --instance=hermes-rpt-control-db --password=<GENERATED> --project=<PROJECT_ID>

# Secrets, service accounts, IAM bindings, WIF pool/provider: see the exact commands this was
# bootstrapped with in git history around the commit that added this file, or re-derive from §2/§3.

# First deploy:
gcloud builds submit --config=<a docker-build-and-push cloudbuild config> .
sed "s|IMAGE_TAG_PLACEHOLDER|<IMAGE_URI>|" docker/cloudrun-service.yaml | \
  gcloud run services replace /dev/stdin --region=us-central1 --project=<PROJECT_ID>
```

## 7. Tearing down

```bash
gcloud run services delete hermes-rpt-api --region=us-central1 --project=hermes-rpt-demo
gcloud sql instances delete hermes-rpt-control-db --project=hermes-rpt-demo
gcloud projects delete hermes-rpt-demo   # deletes everything else (Artifact Registry, secrets,
                                          # service accounts, WIF pool) in one step
```
