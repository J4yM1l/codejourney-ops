# GitHub Actions to Argo CD setup

GitHub-hosted runners validate and build AMD64 candidates from CodeJourney `main`
(changes reach it only through reviewed pull requests), publish immutable GHCR digests, and dispatch them to
`codejourney-ops`. The dedicated `k3s-gitops` runner uses checksum-verified `crane`
to copy those digests to Zot at `192.168.0.45:30080` over TLS (Homelab CA in
`certs/`), logged in as `ci-pusher`. It verifies the digests are equal, then commits
the stage web/API digests and source annotations to `homelab-release-state/main`
(`apps/codejourney/stage/`). Argo CD Application `stage-codejourney-release` owns the
rollout. The pipeline has no write access to `homelab-infra` (homelab-infra ADR-021). Production promotion remains manual.

Required GitHub configuration:

- `CodeJourney` repository secret `GHCR_TOKEN`: dispatch access to
  `codejourney-ops` (fine-grained, Contents read/write on `codejourney-ops`).
- `codejourney-ops` repository secret `GHCR_TOKEN`: read candidate packages.
- `codejourney-ops` environment `GITOPS_TOKEN` (name kept for now; deployment
  branches: `main` only), containing secrets:
  - `RELEASE_STATE_TOKEN`: fine-grained token selecting **only**
    `homelab-release-state`, Contents read/write;
  - `ZOT_PUSH_PASSWORD`: password of Zot user `ci-pusher` (homelab-infra ADR-017).
- No GitHub environment variables are required.

Cluster-side, the runner's registration token is not a GitHub secret. It lives in
homelab-infra `clusters/edge/workloads/ci/runner-secrets.sops.yaml`; see
[README.md](README.md#runner-authentication). `token-expiry.yml` checks every token's
expiry weekly (Mondays 08:00 UTC) and fails 21 days ahead.

Validate a release by confirming both Actions runs succeeded, the release-bot
commit reached `homelab-release-state/main`, `stage-codejourney-release` is
Synced/Healthy, both
Deployments use the expected Zot digests and source SHA, PVCs are Bound, pods are
Ready without restarts, `/` and `/agent` return 200, and GraphQL answers
`{__typename}`. A rerun is safe because images and manifests use immutable digests.

Troubleshooting:

- `Input required and not supplied: token`: bind the job to the environment holding
  `RELEASE_STATE_TOKEN`.
- Checkout 403: ensure the fine-grained token selects `homelab-release-state` and
  grants Contents read/write.
- GHCR `DENIED` or `Bad credentials`: the `GHCR_TOKEN` has expired; rotate it without
  printing it.
- Zot 401: check `ZOT_PUSH_PASSWORD` against the `ci-pusher` entry (homelab-infra
  `docs/runbooks/zot-gitops.md`). Zot TLS errors: the runner must trust
  `certs/homelab-root-ca.crt`.
- Job queued with no runner: check `edge-ci` in Argo CD and the runner pod in the
  `ci` namespace; an expired `ACCESS_TOKEN` shows as a registration failure in its
  log.

The original pre-GitOps setup guide (in-cluster BuildKit builds, direct `kubectl`
deploys, Sealed Secrets) was removed on 2026-09-29. It remains in Git history.
