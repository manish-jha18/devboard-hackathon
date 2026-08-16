# DORA + SOTA delivery

How DevBoard ships: the pipeline, the safety gates, the canary, and the four
DORA metrics. This file describes what is configured, what each piece does, and
how to open it.

Application: <http://loadbalancer.us-west-2.elb.amazonaws.com/>

---

## The delivery loop in one picture

```
  push to feat/full
        │
        ▼
  ┌─────────────────────────────────────────────┐
  │ GATES  (all must pass, run in parallel)     │
  │  lint · tests · secrets · deps              │
  │  Trivy CRITICAL · Kyverno policies · Sonar  │
  └─────────────────────────────────────────────┘
        │
        ▼
  build → push → cosign sign → SBOM attest
        │
        ▼
  gitops-bump  ──writes new tag into git──┐
        │                                 │
        ▼                                 ▼
  deployment-record                  ArgoCD auto-sync
  (GitHub Deployments API)                │
        │                                 ▼
        │                          Argo Rollouts canary
        │                          20% → 50% → 100%
        │                                 │
        │                          Prometheus analysis
        │                          fails? → auto-abort
        ▼                                 │
  dora-exporter ◄─────────────────────────┘
        │
        ▼
  Grafana "DevBoard — DORA Metrics"
```

---

## How to access everything

Everything is ClusterIP. Port-forward from the EC2 box, or tunnel from your
laptop with `ssh -L`.

| What | Command | Open |
|---|---|---|
| **Grafana** — DORA dashboard | `kubectl -n observability port-forward svc/observability-grafana 3000:80` | <http://localhost:3000> · `admin` / `devboard` |
| **ArgoCD** — sync state, deploy history | `kubectl -n argocd port-forward svc/argocd-server 8080:80` | <http://localhost:8080> · `admin` / see below |
| **Argo Rollouts** — live canary progress | `kubectl -n argo-rollouts port-forward svc/argo-rollouts-dashboard 3100:3100` | <http://localhost:3100> |
| **Prometheus** — alerts, raw queries | `kubectl -n observability port-forward svc/observability-prometheus-k-prometheus 9090:9090` | <http://localhost:9090> |
| **DORA exporter** — raw metrics | `kubectl -n dora port-forward svc/dora-exporter 9101:9101` | <http://localhost:9101/metrics> |
| **Deploy history** | — | GitHub → repo → **Environments** → `production` |

ArgoCD password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

The dashboard lives at Grafana → Dashboards → **DevBoard** folder → **DevBoard
— DORA Metrics** (UID `devboard-dora`).

---

## Where everything is configured

| Concern | File |
|---|---|
| Pipeline orchestration | `.github/workflows/devsecops.yml` |
| Image build, sign, SBOM | `.github/workflows/docker-push.yml` |
| Dockerfile lint + Trivy gate | `.github/workflows/docker-scans.yml` |
| Policy check in CI | `.github/workflows/policy-check.yml` |
| Write the deploy event | `.github/workflows/deployment-record.yml` |
| Rollback | `.github/workflows/rollback.yml` |
| Trivy suppressions | `.trivyignore` |
| Kyverno controller | `gitops/argocd/platform/kyverno.yaml` |
| The policies themselves | `gitops/kyverno/policies/` |
| Argo Rollouts controller | `gitops/argocd/platform/argo-rollouts.yaml` |
| Canary strategy | `helm/devboard/templates/frontend-rollout.yaml` |
| Canary analysis queries | `helm/devboard/templates/frontend-analysistemplate.yaml` |
| Canary tuning | `helm/devboard/values.yaml` → `frontend.rollout` |
| DORA exporter code | `dora-exporter/` |
| DORA exporter deploy | `gitops/dora/`, `gitops/argocd/platform/dora-exporter.yaml` |
| Alerts + recording rules | `gitops/observability/manifests/prometheusrule-dora.yaml` |
| Dashboard | `gitops/observability/dashboards/dashboards/dora.json` |

---

## 1 — The pipeline

**Configured in** `.github/workflows/devsecops.yml`.
**Runs on** push and pull request to `feat/full`.

Gates run first and in parallel. Nothing is published unless all of them pass.

| Job | Checks | Blocks the build? |
|---|---|---|
| `code-quality` | eslint · `go fmt` · `go vet` · ruff | yes |
| `code-tests` | `go test` · `npm run test` · `pytest` | yes |
| `secret-scanning` | GitLeaks across full history | yes |
| `docker-checks` | hadolint + **Trivy CRITICAL** | yes |
| `policy-checks` | Kyverno policies vs the rendered chart | yes |
| `dependency-checks` | govulncheck · `npm audit` · pip-audit | no — reports |
| `sonar-qube` | SonarCloud | no — reports |

Once every gate is green, and only on push (never on a PR):

| Job | What it does |
|---|---|
| `docker-push` | builds, pushes, signs with cosign, generates + attests an SBOM, then verifies its own signature |
| `gitops-bump` | writes the new tag into `helm/devboard/values.yaml` and `k8s/`, commits `ci: deploy <tag>` |
| `deployment-record` | records a deployment event on the GitHub Deployments API |

**Why `deployment-record` exists.** In GitOps the pipeline ends with a commit —
the actual deploy happens later, inside ArgoCD. Nothing writes that down.
Counting workflow runs would count builds, including ones that never synced. So
the pipeline records an explicit event, and the DORA exporter reads it back.

**Check a run:**

```bash
gh run list --branch feat/full --limit 5
gh run view <run-id>
```

---

## 2 — Trivy image gate

**Configured in** `.github/workflows/docker-scans.yml`.

| Step | Severity | Behaviour |
|---|---|---|
| Trivy [CRITICAL — blocking] | CRITICAL | **fails the build** |
| Trivy [HIGH — report only] | HIGH | logged, does not block |
| Trivy [SARIF] | HIGH + CRITICAL | uploaded as an artifact |

`ignore-unfixed: true` means only findings with an available fix can block — the
gate never demands something impossible.

HIGH is visible but non-blocking on purpose: base images routinely carry HIGHs
with no fix, and a gate everyone learns to bypass protects nothing.

**To ship past a CRITICAL**, add it to `.trivyignore` with a reason and an
expiry date, so the suppression gets revisited instead of inherited:

```
CVE-2024-00000  # not reachable: we never invoke the affected codec. expires 2026-12-01
```

The file is currently empty, which is the goal.

**How the frontend passes it.** `frontend/Dockerfile` builds with Node and then
serves the output from `dhi.io/nginx:1` — only `dist/` and an nginx config reach
the runtime image. Serving with `vite preview` instead would require the whole
`node_modules` tree at runtime, which drags esbuild's Go binary into production
along with CVEs from a Go toolchain nothing there ever executes. `nginx.conf`
reproduces the two things vite preview did: SPA fallback, and `/api/*` proxied
to `backend:8080` with the prefix stripped. Side effect: 557MB → 50MB.

---

## 3 — Supply chain: signing and SBOM

**Configured in** `.github/workflows/docker-push.yml`. Per image, in order:

1. build and push → capture the immutable **digest**
2. `cosign sign` — keyless, using the workflow's GitHub OIDC identity, recorded
   in the Rekor transparency log
3. `syft` produces an SPDX JSON SBOM → attached with `cosign attest`
4. `cosign verify` — the build checks its own signature against the same rules
   Kyverno uses in-cluster

Everything after step 1 addresses the **digest**, never the tag. Tags are
mutable; signing a tag would sign whatever happens to be there later.

Keyless signing means no private key exists — nothing to leak or rotate. Trust
comes from the workflow identity plus Rekor.

**Verify an image yourself:**

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/manish-jha18/devboard-hackathon/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  <owner>/devboard-frontend:<tag>

# read the SBOM back out of the registry
cosign download attestation <owner>/devboard-frontend:<tag> \
  | jq -r .payload | base64 -d | jq '.predicate.packages | length'
```

---

## 4 — Policy as code

**Controller:** `gitops/argocd/platform/kyverno.yaml` (Kyverno 3.8.2).
**Policies:** `gitops/kyverno/policies/`, deployed by the `kyverno-policies` app.
**Scope:** namespaces `devboard` and `devboard-helm`.
**Mode:** all four are **Audit** — they report violations, they do not block pods.

| Policy | What it requires |
|---|---|
| `verify-image-signatures` | images carry a Sigstore signature from this repo's workflow |
| `disallow-latest-tag` | an immutable tag — no `:latest`, no untagged images |
| `require-probes-and-resources` | every container has a readinessProbe and CPU/memory requests |
| `require-restricted-security-context` | runAsNonRoot · no privilege escalation · drop ALL capabilities |

Each rule is there for a delivery reason, not just hygiene:

- **Signatures** — anyone holding the registry token can push an image; nobody
  can forge a Sigstore identity.
- **No `:latest`** — a mutable tag makes "what is running right now"
  unanswerable, which breaks lead time and rollback at the same time.
- **Readiness probes** — canary analysis judges a rollout by whether new pods
  become ready. Without a probe, "ready" means "the process started", so a
  container that boots and then serves errors passes every canary step.
- **Security context** — Pod Security Standards *restricted*, trimmed to the
  controls these images can actually satisfy.

`failurePolicy: Ignore` on all four: if the Kyverno webhook is unreachable, the
cluster keeps admitting pods. A policy engine that takes the cluster down when
it dies is a bigger risk than the unsigned image it was meant to catch.

**See what it found:**

```bash
kubectl get clusterpolicy
kubectl get policyreport -n devboard -o wide      # pass/fail per resource

# the actual failures, deduplicated
kubectl get policyreport -n devboard -o json \
  | jq -r '.items[].results[] | select(.result=="fail") | "\(.policy)/\(.rule): \(.message)"' \
  | sort -u
```

The same policies run in CI against the rendered Helm chart, so a violation
shows up on the pull request rather than at admission time.

**Switch a policy to blocking** — one at a time, `disallow-latest-tag` first,
`verify-image-signatures` last:

```bash
sed -i 's/failureAction: Audit/failureAction: Enforce/' \
  gitops/kyverno/policies/disallow-latest-tag.yaml
```

---

## 5 — Progressive delivery

**Controller:** `gitops/argocd/platform/argo-rollouts.yaml` (chart 2.41.1, app v1.9.1).
**Rollout:** `helm/devboard/templates/frontend-rollout.yaml`, active while
`frontend.rollout.enabled: true`.

The frontend deploys as a canary:

```
setWeight 20 → pause 2m → setWeight 50 → pause 2m → setWeight 100
```

Analysis runs against Prometheus from the very first step:

| Check | Passes when | Source metric |
|---|---|---|
| `gateway-success-rate` | ≥ 0.95 | `traces_span_metrics_calls_total{service_name="devboard-gateway.devboard"}` |
| `gateway-latency-p95` | ≤ 1500 ms | `traces_span_metrics_duration_milliseconds_bucket` |

Sampled every 60s, 5 times, after a 60s initial delay. Two consecutive breaches
fail the analysis.

**When analysis fails, the rollout aborts itself** and traffic returns to the
previous ReplicaSet. `abortScaleDownDelaySeconds: 30` keeps that ReplicaSet warm,
so the restore is immediate rather than a cold start. No human, no page.

Tune all of it in `helm/devboard/values.yaml` under `frontend.rollout`.

**What the analysis can and cannot see.** This is a *replica-based* canary — no
Gateway API traffic-router plugin is installed, so stable and canary pods share
one Service and traffic splits roughly by pod count. The metrics are therefore
**blended across both versions**. A completely broken canary at 20% weight drags
the blended success rate to about 80%, which is what the 0.95 threshold is set
to catch — but the query cannot attribute an error to a specific version. For
per-version attribution, install the Gateway API plugin, add
`spec.strategy.canary.trafficRouting`, and the thresholds can tighten a lot.

**Watch and control a rollout:**

```bash
kubectl argo rollouts get rollout devboard-frontend -n devboard-helm --watch
kubectl argo rollouts promote devboard-frontend -n devboard-helm    # skip a pause
kubectl argo rollouts undo    devboard-frontend -n devboard-helm    # manual rollback
```

---

## 6 — Rollback and alerts

**Configured in** `.github/workflows/rollback.yml`. Two ways in:

| Entry point | Who | Notes |
|---|---|---|
| `workflow_dispatch` | a person, from the Actions tab | a **reason is required** |
| `repository_dispatch` type `rollback` | Alertmanager | unattended |

A reason is mandatory because a rollback with no recorded reason is a change
failure that never gets counted.

**It rolls back with git, not kubectl.** It reverts the last `ci: deploy` commit
and pushes; ArgoCD syncs the cluster back within ~3 minutes. Using kubectl would
leave git still claiming the broken revision is live, and ArgoCD's `selfHeal`
would faithfully re-deploy the failure.

It also writes two records — the failed deployment (change-failure-rate
numerator) and the rollback deployment (the MTTR stop-clock).

Alert rules, in `gitops/observability/manifests/prometheusrule-dora.yaml`:

| Alert | Fires when |
|---|---|
| `DeployedRevisionFailing` | gateway success rate < 95% for 5m — carries `action: rollback` |
| `RolloutAborted` | canary analysis rejected a version |
| `RolloutError` | controller cannot progress the rollout at all |
| `RolloutStuckPaused` | paused > 30m — usually an inconclusive analysis query |
| `DoraExporterDown` | exporter not scraped for 10m |
| `DoraExporterCannotReachGitHub` | GitHub API poll failing for 15m |
| `NoDeploymentsRecorded` | nothing deployed in the window while the exporter is healthy |

Rollout alerts read the `rollout_phase` gauge, which emits an explicit 0/1
series per phase. The phases the controller emits are `Abort`, `Completed`,
`Error`, `Paused`, `Progressing`, `Timeout`.

**Wiring alerts to automatic rollback.** Alertmanager is currently disabled
(`alertmanager.enabled: false` in `gitops/observability/prometheus-values.yaml`),
so these rules evaluate and show as firing in Prometheus, but nothing routes
them — the `action: rollback` label has no consumer yet. To close the loop, set
`alertmanager.enabled: true`, then:

```bash
kubectl -n observability create secret generic alertmanager-github \
  --from-literal=token=<PAT with repo scope>
```

```yaml
route:
  routes:
    - matchers: [action="rollback"]
      receiver: github-rollback
      group_wait: 0s
receivers:
  - name: github-rollback
    webhook_configs:
      - url: https://api.github.com/repos/manish-jha18/devboard-hackathon/dispatches
        http_config:
          authorization:
            type: Bearer
            credentials_file: /etc/alertmanager/secrets/alertmanager-github/token
```

Until then, rollback runs manually from the Actions tab and the deployment
records are written either way, so MTTR stays measurable.

---

## 7 — DORA metrics

`dora-exporter/` reads the **GitHub Deployments API** and exposes Prometheus
metrics. It runs in the `dora` namespace and is scraped every 60s.

| Metric | Meaning |
|---|---|
| `dora_deployments_total` | successful deployments in the window |
| `dora_deployment_frequency_per_day` | deployment frequency |
| `dora_lead_time_seconds` | **median** commit-authored → deployed |
| `dora_lead_time_p95_seconds` | 95th percentile lead time |
| `dora_change_failure_rate` | fraction needing remediation (0–1) |
| `dora_failed_deployments_total` | deployments marked failed |
| `dora_mttr_seconds` | median failure → rollback |
| `dora_last_deployment_timestamp_seconds` | when the last deploy landed |
| `dora_exporter_last_scrape_success` | 1 = GitHub reachable, 0 = numbers are stale |

Window is 30 days, refreshed every 5 minutes. Both are set in
`gitops/dora/deployment.yaml`.

**How each number is defined**, because the details change the answer:

- **Lead time** runs from the commit's *authored* timestamp, not from when CI
  started. Measuring from CI start would silently delete review latency, which
  is usually most of the number.
- **Change failure rate** counts deployments that *required remediation* — the
  DORA definition. Not alerts, not incidents in general.
- **MTTR** is time to *restore service*, not time to fix the underlying bug.
- **Medians, not means.** One change that sat in review for three weeks should
  not redefine the team's lead time.
- Change failure rate shows **"no data"** rather than 0% when nothing has
  deployed. A green 0% tile over an idle pipeline is the most common way a DORA
  dashboard misleads.

**The dashboard.** Four stat tiles across the top, trends below, pipeline health
at the bottom. Threshold colours encode the Accelerate performance bands — they
are status colours, not series identity:

| Metric | Elite | High | Medium |
|---|---|---|---|
| Deployment frequency | ≥ 1/day | ≥ 1/week | ≥ 1/month |
| Lead time | < 1 day | < 1 week | < 1 month |
| Change failure rate | ≤ 15% | ≤ 30% | ≤ 30% |
| Time to restore | < 1 hour | < 1 day | < 1 week |

**Optional GitHub token.** The exporter works unauthenticated against a public
repo at 60 requests/hour, and spends one call per deployment for its status — so
it will start returning 403 once there is real history:

```bash
kubectl -n dora create secret generic dora-github \
  --from-literal=token=<PAT with repo scope>
kubectl -n dora rollout restart deploy/dora-exporter
```

---

## Current state

| Component | Status |
|---|---|
| CI pipeline | **all 15 jobs green** end to end |
| Trivy gate | passing · 0 CRITICAL on all three images |
| Secret scanning | passing |
| Kyverno + 4 policies | running · PolicyReports generating |
| Supply chain | images signed, SBOMs attested to the registry |
| DORA exporter | running · scraped · **reporting real data** |
| Grafana dashboard | loaded |
| Argo Rollouts controller | running · no Rollout object yet |
| Alertmanager | disabled |

The pipeline has completed a full cycle: gates → build → sign → SBOM →
gitops-bump → deployment record. `helm/devboard/values.yaml` now carries
`manishjha18/devboard-*` at a tag CI built, published and signed.

First recorded deployment:

```
dora_deployments_total             1
dora_deployment_frequency_per_day  0.033
dora_lead_time_seconds             204      commit -> deployed, 3m24s
dora_change_failure_rate           0.0
dora_mttr_seconds                  NaN      no failures yet
```

### Verify the supply chain yourself

```bash
TAG=$(grep -m1 'tag:' helm/devboard/values.yaml | awk '{print $2}')

cosign verify \
  --certificate-identity-regexp '^https://github.com/manish-jha18/devboard-hackathon/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  manishjha18/devboard-frontend:$TAG

cosign download attestation manishjha18/devboard-frontend:$TAG \
  | jq -r .payload | base64 -d | jq '.predicate.packages | length'
```

The signing identity resolves to
`.../.github/workflows/docker-push.yml@refs/heads/feat/full`, recorded in Rekor.

### What is left

**Deploy the Helm stack.** This is the only remaining step, and it is what
gives Argo Rollouts a Rollout to manage. `gitops/argocd/devboard-helm.yaml`
targets namespace `devboard-helm`, so it stands up a **parallel** stack with its
own Gateway and load balancer — the stack running in `devboard` is untouched.

```bash
kubectl apply -f gitops/argocd/devboard-helm.yaml
kubectl -n devboard-helm get gateway devboard-gateway     # its new ELB hostname
kubectl argo rollouts get rollout devboard-frontend -n devboard-helm --watch
```

After that, every push to `feat/full` deploys through the canary, and a failed
canary aborts itself.

**Optionally, enable Alertmanager** so `DeployedRevisionFailing` can trigger
rollback without a human — see [section 6](#6--rollback-and-alerts).

---

## Known issue

The `kyverno` ArgoCD app shows **OutOfSync / Healthy**. It is cosmetic — the
policies load and run normally. The 11 `policies.kyverno.io` CRDs report drift
that ArgoCD diffs but excludes from its sync task list. `ignoreDifferences` and
`RespectIgnoreDifferences=true` do not clear it.

**Do not try to fix it with `Replace=true`** — that recreates the CRDs and takes
every ClusterPolicy with them.
