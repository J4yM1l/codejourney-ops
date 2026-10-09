# codejourney-ops

Release pipeline for [CodeJourney](https://github.com/J4yM1l/CodeJourney). It copies
CI-built images into the homelab Zot registry and hands the stage release to Argo CD
by committing image digests to
[`homelab-release-state`](https://github.com/J4yM1l/homelab-release-state). It cannot
write `homelab-infra` (homelab-infra ADR-021). It holds no Kubernetes manifests.
Every cluster object, including the runner that executes this pipeline, is managed
by Argo CD from [homelab-infra](https://github.com/J4yM1l/homelab-infra).

## Release path (stage)

```text
push or merge to main (CodeJourney, reviewed through a pull request)
  -> GitHub-hosted AMD64 lint, typecheck, test, build, and GHCR publish
  -> repository_dispatch (deploy-stage) to codejourney-ops
  -> k3s-gitops runner copies the exact digests to Zot (crane, TLS, ci-pusher login)
  -> scripts/update-stage-manifests.py commits the digests to homelab-release-state/main
     (apps/codejourney/stage/{web,api}-deployment.yaml)
  -> Argo CD syncs stage-codejourney-release (plain YAML, Deployments only)
  -> production promotion remains manual (homelab-infra runbook codejourney-release)
```

The runner has no privileged builder, no host runtime socket and no Kubernetes
Role. See [PIPELINE_SETUP.md](PIPELINE_SETUP.md) for the required secrets, how to
validate a release, and troubleshooting.

## Other applications

[Book Library](https://github.com/J4yM1l/book-library) uses the same path with its own
workflow, `.github/workflows/book-library-release.yml` (dispatch type
`deploy-book-library-stage`, Zot repository `book-library-stage`, release-state file
`apps/book-library/stage/app-deployment.yaml`), and the generic
`scripts/update-release-image.py`. Walkthrough: homelab-infra
`docs/runbooks/book-library.md`.

## Where things live

| What | Where |
|---|---|
| Pipeline | `.github/workflows/pipeline.yml` |
| Weekly token expiry check | `.github/workflows/token-expiry.yml` |
| Stage manifest updater | `scripts/update-stage-manifests.py` |
| Homelab Root CA (trusted for Zot TLS) | `certs/homelab-root-ca.crt` |
| Self-hosted runner (`ci` namespace, Argo `edge-ci`) | homelab-infra `clusters/edge/workloads/ci/` |
| `dev-testing` environment (Argo `edge-dev-testing`) | homelab-infra `clusters/edge/workloads/dev-testing/` |
| Stage and production CodeJourney | homelab-infra `clusters/{stage,prod}/applications/codejourney/` |

Secrets are SOPS-encrypted in homelab-infra. The Bitnami Sealed Secrets controller
and its keys were removed on 2026-09-29, so the old `k8s/*sealed-secret.yaml` files
(deleted from this repository) can no longer be decrypted.

## Local deploys to `dev-testing`

`deploy.sh` in the CodeJourney repository builds from your working tree, pushes a
`local-<timestamp>` image to Zot as `dev-pusher` and swaps the image on the
`dev-testing` Deployment. It bypasses this pipeline. Prerequisites and the limited
kubeconfig are in homelab-infra `docs/runbooks/codejourney-dev-deploy.md`.

## Runner authentication

The runner registers with `ACCESS_TOKEN`, a fine-grained token limited to this
repository with **Administration: read and write**. It is read at every runner start;
if it expires, the runner cannot re-register after a restart.

- Current token expires **2026-12-28 15:47 UTC**. `token-expiry.yml` warns 21 days
  ahead using the date recorded in the workflow.
- Rotate: create the new token, store it in the operator Keychain, re-encrypt
  `clusters/edge/workloads/ci/runner-secrets.sops.yaml` in homelab-infra, sync
  `edge-ci`, delete the runner pod, then update `RUNNER_TOKEN_EXPIRES` here and
  revoke the old token.
