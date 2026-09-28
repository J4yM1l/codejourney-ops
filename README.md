# Current GitOps release architecture (2026-09-28)

The active stage release path is:

```text
push poc/agent-visualizer-skills
  -> GitHub-hosted AMD64 typecheck, test, build, and GHCR publish
  -> repository_dispatch to codejourney-ops
  -> unprivileged k3s-gitops runner copies exact digests to LAN Zot
  -> release bot updates homelab-infra/main by digest
  -> Argo CD auto-syncs stage-codejourney
  -> production promotion remains manual
```

The runner has no privileged builder, host runtime socket, or Kubernetes deployment
Role. The workflow uses repository `GHCR_TOKEN` secrets and environment
`GITOPS_TOKEN`; ordinary environment variables are not required. The fine-grained
GitOps token selects `homelab-infra` and grants Contents read/write. See
[PIPELINE_SETUP.md](PIPELINE_SETUP.md) for current setup and validation notes.

> The original guide below is retained as implementation history. References to
> direct `kubectl` deployment, BuildKit sidecars, `dev-testing`, or `main` as the
> CodeJourney trigger describe the superseded pipeline.

---

# codejourney-ops

CI/CD pipeline and Kubernetes manifests for the [CodeJourney](https://github.com/CodeJohnie/CodeJourney) app running on a k3s home-lab cluster.

**Stack:** GitHub Actions · Zot OCI registry (NodePort) · Self-hosted runner (k8s pod) · Tailscale ingress · Bitnami Sealed Secrets · Longhorn distributed storage

---

## How it works

There are two ways to deploy — CI for permanent deploys, and a local script for fast iteration.

### CI path (permanent)

```
push to main (CodeJourney repo)
  → .github/workflows/deploy.yml  (dispatch trigger)
    → this repo's pipeline.yml    (runs on self-hosted runner, ~30 min)
      → nerdctl + buildkitd build web image  → Zot registry (tag = commit sha)
      → nerdctl + buildkitd build API image  → Zot registry (tag = commit sha)
      → kubectl apply postgres, redis         (idempotent)
      → kubectl apply k8s/deployment.yaml     (image rendered in via sed)
      → kubectl apply k8s/api-deployment.yaml (image rendered in via sed)
```

The pipeline **applies the full manifests** on every deploy, so changes to
probes, env vars, or resources in `k8s/*.yaml` reach the cluster
automatically — no manual `kubectl apply` needed.

### Local path (fast iteration)

`deploy.sh` in the CodeJourney repo builds images from your **current
working tree** (nothing needs to be committed) and deploys them directly,
bypassing CI. Native arm64 build on a Mac takes ~5 minutes vs ~30 via CI.

```bash
cd ~/CodeJourney
./deploy.sh          # web only (default)
./deploy.sh api      # API only
./deploy.sh all      # both
```

What it does: `podman build` from the working tree → push to Zot tagged
`local-<timestamp>` → `kubectl set image` → wait for rollout.

Prerequisites:
- **podman** with a running machine (`podman machine start`)
- **kubectl** and `~/.kube/config-codejourney` pointing at
  `https://192.168.0.45:6443` (the master's LAN IP)
- Same LAN as the cluster (the registry is `192.168.0.45:30080`)

Caveats:
- Local deploys are **temporary by design**: the next push to `main`
  rebuilds from committed code and overwrites the `local-*` image.
- Unlike CI, the script only swaps the image — manifest changes in
  `k8s/*.yaml` still deploy through the pipeline.

---

## One-time setup

### 1. Create this repo on GitHub

```bash
cd ~/codejourney-ops
git init
git remote add origin https://github.com/CodeJohnie/codejourney-ops.git
git add .
git commit -m "init: ops repo"
git push -u origin main
```

### 2. Create a GitHub Personal Access Token (PAT)

In GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens:

- **Repository access:** both `CodeJourney` and `codejourney-ops`
- **Permissions:**
  - `Contents` → Read
  - `Actions` → Read & Write
  - `Metadata` → Read (auto-selected)

Save the token — you'll use it in steps 4 and 6.

### 3. Apply runner namespace and RBAC to your cluster

```bash
kubectl apply -f runner/namespace.yaml
kubectl apply -f runner/rbac.yaml
```

### 4. Create runner secrets in the cluster

The runner uses a **PAT** (`ACCESS_TOKEN`) for permanent authentication — no expiring registration tokens.

```bash
kubectl create secret generic runner-secrets -n ci \
  --from-literal=ACCESS_TOKEN=<paste-your-PAT-here>
```

### 5. Deploy the runner pod

```bash
kubectl apply -f runner/deployment.yaml
```

Verify it registered:
```bash
kubectl logs -n ci deployment/github-runner -c runner --tail=10
# Should show: "Listening for Jobs"
```

Confirm in GitHub: Settings → Actions → Runners → you should see `k3s-homelab` as **Idle**.

### 6. Add GHCR_TOKEN secret to both repos

In GitHub → each repo → Settings → Secrets and variables → Actions:

| Secret name | Value |
|---|---|
| `GHCR_TOKEN` | The PAT you created in step 2 |

Set it in **both** `CodeJourney` and `codejourney-ops`.

### 7. Seal and apply app secrets

Create sealed secrets for the database and API using kubeseal. The sealing script is at `k8s/seal-api-secret.sh`.

```bash
# On a machine with kubeseal and access to the cluster:
bash k8s/seal-api-secret.sh
kubectl apply -f k8s/sealed-secrets/
```

Two SealedSecrets are required:

| Secret name | Keys |
|---|---|
| `codejourney-db` | `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| `codejourney-api` | `DATABASE_URL`, `JWT_SECRET` |

### 8. Apply one-time k8s manifests

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/tailscale-proxy.yaml
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/redis.yaml
```

The web and API deployments are created by the pipeline on the first push.

### 9. Trigger a deploy

Push any commit to `main` in the CodeJourney repo. Watch it in:
- GitHub → CodeJourney → Actions tab
- `kubectl rollout status deployment/codejourney -n dev-testing`
- `kubectl rollout status deployment/codejourney-api -n dev-testing`

---

## Repo structure

```
.github/workflows/
  pipeline.yml        # CI/CD pipeline (triggered by CodeJourney's deploy.yml)
k8s/
  namespace.yaml
  deployment.yaml       # Web app — IMAGE_PLACEHOLDER substituted at deploy time
  api-deployment.yaml   # NestJS API — API_IMAGE_PLACEHOLDER substituted at deploy time
  postgres.yaml         # PostgreSQL StatefulSet + headless service (Longhorn PVC)
  redis.yaml            # Redis StatefulSet + headless service (Longhorn PVC)
  tailscale-proxy.yaml  # Tailscale proxy Deployment + RBAC (state in "tailscale" Secret)
  ingress-tailscale.yaml
  sealed-secret.yaml    # Encrypted web-app secrets (safe to commit)
  seal-api-secret.sh    # Script to generate and seal DB + API secrets
runner/
  namespace.yaml        # ci namespace
  rbac.yaml             # ServiceAccount + Role scoped to dev-testing
  deployment.yaml       # Runner pod (github-runner + buildkitd sidecar)
```

---

## Runner authentication

The runner uses **PAT-based authentication** (`ACCESS_TOKEN`). Unlike the one-time registration tokens, the PAT auto-refreshes on every restart — no token rotation needed. If you ever revoke and regenerate the PAT:

```bash
kubectl create secret generic runner-secrets -n ci \
  --from-literal=ACCESS_TOKEN=<new-PAT> \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/github-runner -n ci
```

---

## What's running in `dev-testing`

| Workload | Type | Storage |
|---|---|---|
| `codejourney` | Deployment (Next.js) | — |
| `codejourney-api` | Deployment (NestJS/GraphQL) | — |
| `postgres` | StatefulSet | 5Gi Longhorn PVC |
| `redis` | StatefulSet | 1Gi Longhorn PVC |
| `tailscale-proxy` | Deployment | state in `tailscale` Secret (no PVC) |

**Access:**
- Web: `https://codejourney.tail9d71dd.ts.net` (via tailscale-proxy)
- API: cluster-internal only (`codejourney-api.dev-testing.svc.cluster.local:4000`).
  Browsers reach it through the web app's same-origin `/api/graphql` proxy
  route, which forwards to the API service — so the API needs no public
  hostname and no CORS config.

**Why the tailscale-proxy has no PVC:** tailscale's containerboot stores its
real state (machine key, TLS certs, device identity) in the `tailscale`
Kubernetes Secret (`TS_KUBE_SECRET`). The old Longhorn PVC only ever held
logs, and its volume faulting caused Longhorn to repeatedly delete the pod.
