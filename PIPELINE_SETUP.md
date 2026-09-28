# Current GitHub Actions to Argo CD setup (2026-09-28)

The current workflow no longer builds on the homelab runner or applies Kubernetes
manifests directly. GitHub-hosted runners validate and build AMD64 candidates from
`poc/agent-visualizer-skills`, publish immutable GHCR digests, and dispatch them to
`codejourney-ops`. The dedicated `k3s-gitops` runner uses checksum-verified `crane`
to copy those digests to `192.168.0.45:30080`, verifies equality, and commits the
stage web/API digest and source annotations to `homelab-infra/main`. Argo CD owns
the cluster rollout. Production promotion remains manual.

Required GitHub configuration:

- `CodeJourney` repository secret `GHCR_TOKEN`: dispatch access to
  `codejourney-ops`.
- `codejourney-ops` repository secret `GHCR_TOKEN`: read candidate packages.
- `codejourney-ops` environment `GITOPS_TOKEN`, containing secret `GITOPS_TOKEN`:
  fine-grained token selecting `homelab-infra`, Contents read/write.
- No GitHub environment variables are required.

Validate a release by confirming both Actions runs succeeded, the release-bot
commit reached `homelab-infra/main`, `stage-codejourney` is Synced/Healthy, both
Deployments use the expected Zot digests and source SHA, PVCs are Bound, pods are
Ready without restarts, `/` and `/agent` return 200, and GraphQL answers
`{__typename}`. A rerun is safe because images and manifests use immutable digests.

Troubleshooting:

- `Input required and not supplied: token`: bind the job to the environment holding
  `GITOPS_TOKEN`.
- Checkout 403: ensure the fine-grained token selects `homelab-infra` and grants
  Contents read/write.
- GHCR failure: rotate `GHCR_TOKEN` without printing it.
- Zot failure: verify registry health and trusted-LAN reachability.

> The complete original setup guide below is retained as implementation history.
> Its direct-deployment, privileged BuildKit, and `dev-testing` instructions are
> superseded and must not be applied to the current GitOps pipeline.

---

# CodeJourney CI/CD Pipeline — Complete Setup Guide

> **What this guide does:** Every time you push code to the `main` branch of CodeJourney, it automatically builds Docker images for the web app and API, pushes them to your home-lab registry, applies any infrastructure changes, and rolls out both services to your Kubernetes cluster — all for free, with zero manual steps.

---

## Table of Contents

1. [How It Works — The Big Picture](#1-how-it-works--the-big-picture)
2. [Architecture Diagram](#2-architecture-diagram)
3. [What You Need Before Starting](#3-what-you-need-before-starting)
4. [Your Cluster Layout](#4-your-cluster-layout)
5. [Step-by-Step Setup](#5-step-by-step-setup)
   - [Step 1 — Create the Ops Repo on GitHub](#step-1--create-the-ops-repo-on-github)
   - [Step 2 — Create a GitHub Personal Access Token (PAT)](#step-2--create-a-github-personal-access-token-pat)
   - [Step 3 — Apply Kubernetes Namespaces and RBAC](#step-3--apply-kubernetes-namespaces-and-rbac)
   - [Step 4 — Create Cluster Secrets](#step-4--create-cluster-secrets)
   - [Step 5 — Deploy the Self-Hosted Runner](#step-5--deploy-the-self-hosted-runner)
   - [Step 6 — Add GitHub Secrets to Both Repos](#step-6--add-github-secrets-to-both-repos)
   - [Step 7 — Seal and Apply App Secrets](#step-7--seal-and-apply-app-secrets)
   - [Step 8 — Apply One-Time Kubernetes Manifests](#step-8--apply-one-time-kubernetes-manifests)
   - [Step 9 — Push to Main and Watch It Deploy](#step-9--push-to-main-and-watch-it-deploy)
6. [All the Files — What Each One Does](#6-all-the-files--what-each-one-does)
7. [Troubleshooting](#7-troubleshooting)
8. [Maintenance](#8-maintenance)

---

## 1. How It Works — The Big Picture

```
YOU                 GITHUB                    YOUR HOME-LAB CLUSTER
───                 ──────                    ─────────────────────

git push main  ──►  CodeJourney repo          ┌─────────────────────────────────────┐
                    (.github/workflows/        │  ci namespace                       │
                     deploy.yml)              │                                     │
                         │                   │  ┌──────────────────────┐           │
                         │  repository_      │  │  Runner Pod          │           │
                         │  dispatch event   │  │  ┌────────────────┐  │           │
                         ▼                   │  │  │  github-runner │  │           │
                    codejourney-ops repo  ──► │  │  └────────────────┘  │           │
                    (.github/workflows/    │  │  │  ┌────────────────┐  │           │
                     pipeline.yml)        └──┼──┼─►│  buildkitd     │  │           │
                                             │  │  └────────────────┘  │           │
                                             │  └──────────────────────┘           │
                                             │           │                          │
                                             │    ┌──────▼──────┐                  │
                                             │    │  Zot Registry│                  │
                                             │    │  :30080      │                  │
                                             │    └──────┬───────┘                  │
                                             │           │ (web image + API image)  │
                                             │    ┌──────▼───────────────────────┐  │
                                             │    │  dev-testing namespace        │  │
                                             │    │  codejourney (Next.js)       │  │
                                             │    │  codejourney-api (NestJS)    │  │
                                             │    │  postgres  redis             │  │
                                             │    └──────────────────────────────┘  │
                                             └─────────────────────────────────────┘
```

**In plain English:**
1. You push code to `main` in CodeJourney
2. GitHub runs a small job that sends a dispatch event to `codejourney-ops` with the git SHA
3. Your self-hosted runner (a pod in your cluster) picks up the job
4. It builds the Next.js web image and the NestJS API image using BuildKit
5. Both images are pushed to your Zot registry
6. Postgres and Redis manifests are applied (idempotent — nothing changes if they're already running)
7. Kubernetes rolls out the new web and API images
8. Both services are live with the new code — usually in under 5 minutes

**Why two repos?** All CI/CD logic lives in `codejourney-ops`. CodeJourney only has a 10-line trigger. This keeps the app repo clean and means you never need to touch pipeline code when changing the app.

---

## 2. Architecture Diagram

### Infrastructure Layout

```
┌─────────────────────────────────────────────────────────────────┐
│                    HOME-LAB NETWORK (192.168.0.x)               │
│                                                                 │
│  ┌─────────────────┐   ┌──────────────┐   ┌──────────────┐    │
│  │   rpi-master     │   │   oddnode    │   │    odnode    │    │
│  │  192.168.0.45   │   │ 192.168.0.46 │   │ 192.168.0.47 │    │
│  │  k3s control    │   │  k3s worker  │   │  k3s worker  │    │
│  │  plane + Zot    │   │  ARM64       │   │  ARM64       │    │
│  │  :30080         │   │              │   │              │    │
│  └────────┬────────┘   └──────┬───────┘   └──────┬───────┘    │
│           └───────────────────┴───────────────────┘            │
│                               │                                 │
│                    ┌──────────▼──────────┐                     │
│                    │   k3s cluster        │                     │
│          ┌─────────┴──────────────────────────────────┐        │
│          │                                            │        │
│   ┌──────▼────────┐  ┌──────────┐  ┌────────────────▼──────┐  │
│   │  ci namespace  │  │ registry │  │   dev-testing namespace│  │
│   │               │  │ namespace│  │                        │  │
│   │ github-runner  │  │  Zot     │  │ codejourney (Next.js) │  │
│   │ (pod)         │  │ :30080   │  │ codejourney-api       │  │
│   │ + buildkitd   │  │          │  │   (NestJS/GraphQL)    │  │
│   │ (sidecar)     │  │          │  │ postgres (StatefulSet)│  │
│   └───────────────┘  └──────────┘  │ redis    (StatefulSet)│  │
│                                    │ tailscale-proxy       │  │
│                                    └───────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                               │
                        Tailscale VPN
                               │
              ┌────────────────┴────────────────────┐
              │  codejourney.tail9d71dd.ts.net       │  (web)
              │  codejourney-api.tail9d71dd.ts.net   │  (API)
              └─────────────────────────────────────┘
```

### Pipeline Flow (Step by Step)

```
┌──────────────────────────────────────────────────────────────────────┐
│                          PIPELINE FLOW                               │
│                                                                      │
│  1. git push origin main                                             │
│         │                                                            │
│         ▼                                                            │
│  2. GitHub Actions — CodeJourney repo                                │
│     deploy.yml sends repository_dispatch → codejourney-ops          │
│     Payload: { sha: "<git commit SHA>" }     ⏱ ~10 seconds          │
│         │                                                            │
│         ▼                                                            │
│  3. pipeline.yml triggered on self-hosted runner (k3s pod)           │
│         │                                                            │
│         ├──► Checkout CodeJourney source (ref: main)                │
│         │                                                            │
│         ├──► Install nerdctl + buildctl (ARM64 binaries)             │
│         │                                                            │
│         ├──► Wait for buildkitd socket to be ready                  │
│         │                                                            │
│         ├──► nerdctl build -f Dockerfile     → web image             │
│         │    nerdctl push  → 192.168.0.45:30080/codejourney:<sha>   │
│         │    ⏱ ~90s (cached) / ~5min (cold)                         │
│         │                                                            │
│         ├──► nerdctl build -f Dockerfile.api → API image             │
│         │    nerdctl push  → 192.168.0.45:30080/codejourney-api:<sha>│
│         │    ⏱ ~90s (cached) / ~5min (cold)                         │
│         │                                                            │
│         ├──► Install kubectl                                         │
│         │                                                            │
│         ├──► Checkout codejourney-ops (for k8s manifests)           │
│         │                                                            │
│         ├──► kubectl apply postgres.yaml, redis.yaml  (idempotent)  │
│         │                                                            │
│         ├──► kubectl set image deployment/codejourney                │
│         │    kubectl rollout status --timeout=120s                   │
│         │                                                            │
│         └──► kubectl set image deployment/codejourney-api            │
│              kubectl rollout status --timeout=120s                   │
│                                                                      │
│  TOTAL: ~4-6 minutes per deploy                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Runner Pod Architecture

```
┌─────────────────────────────────────────────────────┐
│              Runner Pod (ci namespace)               │
│                                                     │
│  ┌─────────────────────┐  ┌──────────────────────┐ │
│  │   github-runner      │  │     buildkitd        │ │
│  │   container          │  │   (privileged)       │ │
│  │                     │  │                     │ │
│  │  Runs GitHub Actions │  │  Builds Docker       │ │
│  │  workflow steps      │  │  images without      │ │
│  │                     │  │  a Docker daemon     │ │
│  │  BUILDKIT_HOST ─────┼──┼─► unix socket        │ │
│  │  unix:///run/        │  │                     │ │
│  │  buildkit/           │  │  Shared socket:     │ │
│  │  buildkitd.sock      │  │  /run/buildkit/     │ │
│  └──────────┬───────────┘  └─────────────────────┘ │
│             │                                       │
│   Shared volumes:                                   │
│   /run/buildkit/buildkitd.sock  (emptyDir)          │
│   /run/k3s/containerd/containerd.sock (hostPath)    │
│                                                     │
│   ServiceAccount: github-runner                     │
│   Auth: ACCESS_TOKEN (PAT) — auto-refreshes         │
│   RBAC: Role "deployer" in dev-testing namespace    │
└─────────────────────────────────────────────────────┘
```

---

## 3. What You Need Before Starting

| # | What | Why |
|---|------|-----|
| 1 | A GitHub account | Free at github.com |
| 2 | Two GitHub repos | `CodeJourney` (app) and `codejourney-ops` (pipeline) |
| 3 | A k3s cluster running | 1 control plane + 2 worker nodes |
| 4 | SSH access to cluster nodes | e.g. `ssh rpi-main` works from your Mac |
| 5 | Zot container registry | NodePort `30080`, HTTP (insecure) — configured in `registries.yaml` on each node |
| 6 | Bitnami Sealed Secrets operator | For encrypted secrets committed to git |
| 7 | Tailscale operator installed | For remote access via VPN |
| 8 | Longhorn storage operator | Distributed block storage for postgres, redis, and tailscale-proxy PVCs |
| 9 | `kubectl` on your Mac | `brew install kubectl` |
| 10 | `kubeseal` on your Mac | `brew install kubeseal` |

### Cost: $0

- **GitHub Actions** — free tier (unlimited for self-hosted runners)
- **Zot registry** — open source, runs in your cluster
- **BuildKit** — open source image builder

---

## 4. Your Cluster Layout

| Node | IP | Role | OS |
|------|-----|------|----|
| `rpi-master` | `192.168.0.45` | control-plane + Zot NodePort | Ubuntu 24.04 ARM64 |
| `oddnode` | `192.168.0.46` | worker | Ubuntu 24.04 ARM64 |
| `odnode` | `192.168.0.47` | worker | Ubuntu 24.04 ARM64 |

| Namespace | What lives there |
|-----------|-----------------|
| `ci` | GitHub Actions self-hosted runner pod |
| `registry` | Zot container registry (NodePort 30080) |
| `dev-testing` | CodeJourney web, NestJS API, PostgreSQL, Redis, Tailscale proxy |

**SSH aliases:**

```
ssh rpi-main   → 192.168.0.45 (control plane)
ssh mainod     → 192.168.0.46 (worker)
ssh dev        → 192.168.0.47 (worker)
```

> **Note:** `rpi-master` has a `CriticalAddonsOnly=true:NoSchedule` taint. Regular pods land on worker nodes automatically.

---

## 5. Step-by-Step Setup

---

### Step 1 — Create the Ops Repo on GitHub

Go to [github.com/new](https://github.com/new) and create `codejourney-ops` as **Public**.

```bash
cd ~/codejourney-ops
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/codejourney-ops.git
git push -u origin main
```

---

### Step 2 — Create a GitHub Personal Access Token (PAT)

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**
2. Click **Generate new token**
3. Name it `codejourney-pipeline`
4. **Repository access** → select both `CodeJourney` and `codejourney-ops`
5. **Permissions:**
   - `Contents` → Read
   - `Actions` → Read and Write
   - `Metadata` → Read (auto-selected)
6. Click **Generate token** and copy it immediately

> This PAT is used both as the runner's `ACCESS_TOKEN` (permanent registration auth) and as `GHCR_TOKEN` (repo checkout auth in the pipeline).

---

### Step 3 — Apply Kubernetes Namespaces and RBAC

```bash
kubectl apply -f runner/namespace.yaml
kubectl apply -f runner/rbac.yaml
```

This creates the `ci` namespace, a `ServiceAccount` for the runner, and a scoped `Role` that lets the runner deploy to `dev-testing`.

---

### Step 4 — Create Cluster Secrets

The runner authenticates with GitHub using a **PAT** (`ACCESS_TOKEN`). Unlike registration tokens, the PAT auto-refreshes on every runner restart — no expiry, no rotation needed.

```bash
kubectl create secret generic runner-secrets -n ci \
  --from-literal=ACCESS_TOKEN=<paste-your-PAT-here>
```

**Verify:**
```bash
kubectl get secret runner-secrets -n ci
```

---

### Step 5 — Deploy the Self-Hosted Runner

The runner pod has two containers:
- **`github-runner`** — executes workflow steps, authenticates via `ACCESS_TOKEN`
- **`buildkitd`** — privileged container that builds Docker images without a Docker daemon

```bash
kubectl apply -f runner/deployment.yaml
```

**Wait for both containers (2/2):**
```bash
kubectl rollout status deployment/github-runner -n ci --timeout=300s
```

**Verify registration:**
```bash
kubectl logs -n ci deployment/github-runner -c runner --tail=10
```

You should see:
```
√ Runner successfully added
√ Settings Saved.
Listening for Jobs
```

Confirm in GitHub: `codejourney-ops` → Settings → Actions → Runners → `k3s-homelab` should show **Idle**.

---

### Step 6 — Add GitHub Secrets to Both Repos

The PAT must be set as `GHCR_TOKEN` in both repos so the pipeline can check out source code.

```bash
gh secret set GHCR_TOKEN --body "<your-PAT>" --repo YOUR_USERNAME/codejourney-ops
gh secret set GHCR_TOKEN --body "<your-PAT>" --repo YOUR_USERNAME/CodeJourney
```

---

### Step 7 — Seal and Apply App Secrets

The app uses two SealedSecrets:

| Secret name | Keys | Used by |
|---|---|---|
| `codejourney-db` | `POSTGRES_USER`, `POSTGRES_PASSWORD` | PostgreSQL StatefulSet |
| `codejourney-api` | `DATABASE_URL`, `JWT_SECRET` | NestJS API Deployment |

Use `k8s/seal-api-secret.sh` to create and seal them:

```bash
# Set your values as env vars first:
export POSTGRES_USER=codejourney
export POSTGRES_PASSWORD=<strong-password>
export JWT_SECRET=<strong-secret>
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres.dev-testing.svc.cluster.local:5432/codejourney"

bash k8s/seal-api-secret.sh
```

The script produces sealed YAML files. Apply them:
```bash
kubectl apply -f k8s/sealed-secrets/
```

> Sealed secrets are encrypted with the cluster's public key — safe to commit to git.

---

### Step 8 — Apply One-Time Kubernetes Manifests

These create the namespace, storage, and persistent services. Run once; the pipeline handles everything after this.

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/tailscale-proxy.yaml
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/redis.yaml
```

**Verify everything is up:**
```bash
kubectl get pods -n dev-testing
# Expected:
#   postgres-0         1/1   Running
#   redis-0            1/1   Running
#   tailscale-proxy-0  1/1   Running
```

---

### Step 9 — Push to Main and Watch It Deploy

Trigger the first automated deploy:

```bash
cd ~/CodeJourney
git commit --allow-empty -m "ci: trigger first automated deploy"
git push origin main
```

**Watch the pipeline:**
```bash
# List recent pipeline runs
gh run list --repo YOUR_USERNAME/codejourney-ops --limit 5

# Watch the web rollout
kubectl rollout status deployment/codejourney -n dev-testing

# Watch the API rollout
kubectl rollout status deployment/codejourney-api -n dev-testing
```

**What success looks like:**
```
✓ Checkout CodeJourney source
✓ Install nerdctl and buildctl
✓ Wait for buildkitd
✓ Build and push web image to Zot
✓ Build and push API image to Zot
✓ Install kubectl
✓ Checkout ops repo (for k8s manifests)
✓ Apply infrastructure (postgres, redis) — idempotent
✓ Deploy web app
✓ Deploy API
```

---

## 6. All the Files — What Each One Does

### `codejourney-ops` repo structure

```
codejourney-ops/
│
├── .github/
│   └── workflows/
│       └── pipeline.yml            ← Main CI/CD pipeline
│                                      Builds web + API images
│                                      Applies postgres + redis (idempotent)
│                                      Rolls out both deployments
│
├── k8s/
│   ├── namespace.yaml              ← Creates dev-testing namespace
│   ├── deployment.yaml             ← Next.js web app (IMAGE_PLACEHOLDER → sha at deploy)
│   ├── api-deployment.yaml         ← NestJS API (API_IMAGE_PLACEHOLDER → sha at deploy)
│   ├── postgres.yaml               ← PostgreSQL StatefulSet + headless service
│   │                                  5Gi Longhorn PVC, reads from codejourney-db secret
│   ├── redis.yaml                  ← Redis StatefulSet + headless service
│   │                                  1Gi Longhorn PVC, AOF persistence enabled
│   ├── tailscale-proxy.yaml        ← Tailscale proxy StatefulSet + RBAC
│   │                                  100Mi Longhorn PVC
│   ├── ingress-tailscale.yaml      ← Tailscale ingress for web app
│   ├── sealed-secret.yaml          ← Encrypted web-app secrets (safe to commit)
│   └── seal-api-secret.sh          ← Script to generate + seal DB + API secrets
│
└── runner/
    ├── namespace.yaml              ← Creates ci namespace
    ├── rbac.yaml                   ← ServiceAccount + Role (scoped to dev-testing)
    └── deployment.yaml             ← Runner pod
                                       Container 1: github-runner (ACCESS_TOKEN auth)
                                       Container 2: buildkitd (privileged, image builder)
```

### `CodeJourney` repo — the only CI/CD file

```
CodeJourney/
├── .github/
│   └── workflows/
│       └── deploy.yml              ← Sends repository_dispatch on push to main
│                                      Payload: { sha: "<commit SHA>" }
├── Dockerfile                      ← Next.js standalone build (linux/arm64)
└── Dockerfile.api                  ← NestJS API multi-stage build
                                       Stages: base → deps → builder → prod-deps → runner
                                       Runner: node:22-slim (Debian, for OpenSSL 3.x)
                                       Prisma binaryTargets: linux-arm64-openssl-3.0.x
```

### Key pipeline sections explained

#### Web and API image build

```yaml
- name: Build and push web image to Zot
  run: |
    IMAGE="${{ env.REGISTRY }}/${{ env.APP }}:${{ github.event.client_payload.sha }}"
    LATEST="${{ env.REGISTRY }}/${{ env.APP }}:latest"
    nerdctl --insecure-registry build --platform linux/arm64 -t "${IMAGE}" -t "${LATEST}" -f Dockerfile .
    nerdctl --insecure-registry push "${IMAGE}"
    nerdctl --insecure-registry push "${LATEST}"

- name: Build and push API image to Zot
  run: |
    IMAGE="${{ env.REGISTRY }}/${{ env.API_APP }}:${{ github.event.client_payload.sha }}"
    LATEST="${{ env.REGISTRY }}/${{ env.API_APP }}:latest"
    nerdctl --insecure-registry build --platform linux/arm64 -t "${IMAGE}" -t "${LATEST}" -f Dockerfile.api .
    nerdctl --insecure-registry push "${IMAGE}"
    nerdctl --insecure-registry push "${LATEST}"
```

#### Idempotent infrastructure apply

```yaml
- name: Apply infrastructure (postgres, redis) — idempotent
  run: |
    kubectl apply -f ops/k8s/postgres.yaml
    kubectl apply -f ops/k8s/redis.yaml
```

#### Rolling deploy with create-on-first-run

```yaml
- name: Deploy API
  run: |
    API_IMAGE="${{ env.REGISTRY }}/${{ env.API_APP }}:${{ github.event.client_payload.sha }}"
    if kubectl get deployment ${{ env.API_APP }} -n dev-testing &>/dev/null; then
      kubectl set image deployment/${{ env.API_APP }} ${{ env.API_APP }}="${API_IMAGE}" -n dev-testing
    else
      sed "s|API_IMAGE_PLACEHOLDER|${API_IMAGE}|g" ops/k8s/api-deployment.yaml | kubectl apply -f -
    fi
    kubectl rollout status deployment/${{ env.API_APP }} -n dev-testing --timeout=120s
```

### API environment variables

| Variable | Source | Value |
|---|---|---|
| `DATABASE_URL` | SealedSecret `codejourney-api` | `postgresql://...@postgres.dev-testing.svc.cluster.local:5432/codejourney` |
| `JWT_SECRET` | SealedSecret `codejourney-api` | your secret |
| `REDIS_URL` | plain env | `redis://redis.dev-testing.svc.cluster.local:6379` |
| `WEB_URL` | plain env | `https://codejourney.tail9d71dd.ts.net` |
| `NODE_ENV` | plain env | `production` |
| `PORT` | plain env | `4000` |

---

## 7. Troubleshooting

### Problem: Runner pod is in `CrashLoopBackOff` with `couldn't find key ACCESS_TOKEN`

**Cause:** The `runner-secrets` secret was created with `RUNNER_TOKEN` (old key name) instead of `ACCESS_TOKEN`.

**Fix:**
```bash
kubectl delete secret runner-secrets -n ci
kubectl create secret generic runner-secrets -n ci \
  --from-literal=ACCESS_TOKEN=<your-PAT>
kubectl rollout restart deployment/github-runner -n ci
```

---

### Problem: Pipeline job is "queued" for more than 5 minutes

**Cause:** The runner lost its GitHub connection.

**Fix:**
```bash
kubectl rollout restart deployment/github-runner -n ci
kubectl logs -n ci deployment/github-runner -c runner -f
# Wait for: "Listening for Jobs"
```

---

### Problem: `ErrImagePull` — can't pull image from Zot

**Cause:** The Zot NodePort uses HTTP, but k3s expects HTTPS unless configured otherwise.

**Fix:** Ensure `/etc/rancher/k3s/registries.yaml` exists on **every node**:

```yaml
mirrors:
  "192.168.0.45:30080":
    endpoint:
      - "http://192.168.0.45:30080"
```

After editing, restart k3s on each node:
```bash
ssh rpi-main "sudo systemctl restart k3s"
ssh mainod   "sudo systemctl restart k3s-agent"
ssh dev      "sudo systemctl restart k3s-agent"
```

---

### Problem: API pod crashes — `PrismaClientInitializationError`

**Cause:** Prisma client was generated for the wrong binary target (Alpine builder vs. Debian runtime).

**Check:** The `apps/api/prisma/schema.prisma` in CodeJourney must have:
```prisma
generator client {
  provider      = "prisma-client-js"
  binaryTargets = ["native", "linux-arm64-openssl-3.0.x"]
}
```

`linux-arm64-openssl-3.0.x` matches the `node:22-slim` (Debian) runtime used in `Dockerfile.api`'s final stage. The native target covers the Alpine builder stages.

---

### Problem: `kubectl rollout status` times out

**Cause:** The new pod isn't becoming Ready. Check pod events and logs:

```bash
kubectl describe pod -n dev-testing -l app=codejourney-api | tail -30
kubectl logs -n dev-testing -l app=codejourney-api --tail=50
```

Common causes:
- Secret not found → check SealedSecret was applied and decrypted
- Postgres not ready → check `kubectl get pods -n dev-testing`
- Prisma binary target mismatch → see entry above

---

### Problem: `watch` permission denied on rollout status

**Cause:** The runner's RBAC Role is missing the `watch` verb for deployments.

**Fix:**
```bash
kubectl apply -f runner/rbac.yaml
```

---

### Problem: nerdctl build fails — "buildkitd not running"

**Cause:** The buildkitd sidecar hasn't started yet.

```bash
kubectl logs -n ci $(kubectl get pod -n ci -l app=github-runner -o name) -c buildkitd
```

The pipeline's `Wait for buildkitd` step polls until the socket is ready. If the sidecar is stuck, restart the runner pod:
```bash
kubectl rollout restart deployment/github-runner -n ci
```

---

### Problem: Multiple stale ReplicaSets piling up

This can happen when the API rollout times out repeatedly. Clean them up:

```bash
# List them
kubectl get rs -n dev-testing -l app=codejourney-api

# Delete old ones (keep only the current active one)
kubectl delete rs -n dev-testing <old-rs-name-1> <old-rs-name-2> ...
```

---

## 8. Maintenance

### Runner authentication

The runner uses a **PAT** (`ACCESS_TOKEN`) — it auto-refreshes on every restart. No regular maintenance needed. If you ever revoke and regenerate the PAT:

```bash
kubectl create secret generic runner-secrets -n ci \
  --from-literal=ACCESS_TOKEN=<new-PAT> \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/github-runner -n ci
```

### Prisma migrations

Database migrations run automatically at API startup via:
```
node $(find node_modules -path '*/prisma/build/index.js' | head -1) migrate deploy
```

To add a new migration, run `pnpm prisma migrate dev` locally in `apps/api/`, commit the migration file, and push — the pipeline will apply it on the next deploy.

---

## Quick Reference

```bash
# Check all services are healthy
kubectl get pods -n dev-testing

# Watch a deploy live
kubectl rollout status deployment/codejourney -n dev-testing -w
kubectl rollout status deployment/codejourney-api -n dev-testing -w

# View web app logs
kubectl logs -n dev-testing -l app=codejourney -f --tail=50

# View API logs
kubectl logs -n dev-testing -l app=codejourney-api -f --tail=50

# Check runner is listening
kubectl logs -n ci deployment/github-runner -c runner --tail=5

# Roll back web app
kubectl rollout undo deployment/codejourney -n dev-testing

# Roll back API
kubectl rollout undo deployment/codejourney-api -n dev-testing

# Force redeploy without a code change
cd ~/CodeJourney
git commit --allow-empty -m "ci: force redeploy"
git push origin main

# List recent pipeline runs
gh run list --repo YOUR_USERNAME/codejourney-ops --limit 5
```
