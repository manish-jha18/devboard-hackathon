# DORA + SOTA — what was built, what it checks, how to reach it

Single reference for the DORA and progressive-delivery layer added to DevBoard:
what exists, what each piece actually checks, how to reach it, and where the
honest limits are.

Application: <http://a1e028058687441b2bbb7d22ccc5cfb7-1032811035.us-west-2.elb.amazonaws.com/>

---

## Three things that were broken first

None of the four DORA numbers can mean anything until these are true. All three
are fixed; they are recorded here because they explain why the metrics read the
way they do.

**The pipeline had never run.** `devsecops.yml` triggered on
`branches: [mega-project]` — a branch that does not exist in this fork. Every
gate, build and image push was dormant. Retargeted to `feat/full`, along with
`terraform-ci.yml`.

**Nothing consumed the pipeline's output.** `gitops-bump` writes the new image
tag into `helm/devboard/values.yaml` and `k8s/`, expecting ArgoCD to notice. But
the running stack was applied by hand with `kubectl apply -f k8s/` — no
`argocd.argoproj.io/instance` label, no Application watching it. CI could have
pushed images forever without deploying one.

**Every gate was advisory.** Trivy, Checkov and the IaC scan all ran with
`continue-on-error: true`, so a CRITICAL CVE produced a green tick. Scanning
without gating is a dashboard, not a control.

---

## Status against the checklist

Verified against the live cluster, not assumed. "Installed" and "producing
data" are tracked separately on purpose — a component that is running but has
never been exercised is not the same as one that works.

| # | Requirement | Built | Live | Producing data |
|---|---|---|---|---|
| 1 | Auto-sync CI/CD | yes | yes | pipeline runs; **deploy step blocked, see below** |
| 1 | Visible deploy history | yes | yes | no records yet — nothing has deployed |
| 2 | Automated rollback | yes | controller live | never exercised — no Rollout deployed |
| 2 | Alerting tied to rollback | rules yes | **Alertmanager is disabled** | alerts evaluate, nothing routes them |
| 3 | Progressive delivery (Argo Rollouts) | yes | controller live | **no Rollout object exists yet** |
| 4 | Policy as code (Kyverno) | yes | yes | 4 policies, 13 PolicyReports, 36 pass / 39 fail |
| 4 | Trivy scan gate in CI | yes | yes | **actively blocking — working as designed** |
| 5 | cosign signing + SBOM | yes | n/a | never executed — blocked behind the Trivy gate |
| 6 | DORA metrics dashboard | yes | yes | exporter scraped; all four keys at 0 / NaN |

**Short version:** every component is installed and verified running. Three
capabilities have not produced data yet, for two specific reasons:

1. **The Trivy CRITICAL gate is blocking `docker-push`.** That means cosign
   signing, SBOM attestation, `gitops-bump` and `deployment-record` are all
   skipped, so DORA has nothing to count. This is the gate doing its job, not a
   defect — see [Unblocking the pipeline](#unblocking-the-pipeline).
2. **The Helm stack is not deployed.** `gitops/argocd/devboard-helm.yaml` has
   never been applied, so no `Rollout` object exists and the canary has nothing
   to canary. The running app was applied by hand from `k8s/`.

---

## Quick access

Everything is ClusterIP. Port-forward from the EC2 box, or tunnel from your
laptop with `ssh -L`.

| What | Command | Then open |
|---|---|---|
| **Grafana** (DORA dashboard) | `kubectl -n observability port-forward svc/observability-grafana 3000:80` | <http://localhost:3000> — user `admin`, password `devboard` |
| **ArgoCD** | `kubectl -n argocd port-forward svc/argocd-server 8080:80` | <http://localhost:8080> — user `admin`, password below |
| **Argo Rollouts** | `kubectl -n argo-rollouts port-forward svc/argo-rollouts-dashboard 3100:3100` | <http://localhost:3100> |
| **Prometheus** | `kubectl -n observability port-forward svc/observability-prometheus-k-prometheus 9090:9090` | <http://localhost:9090> |
| **DORA exporter raw** | `kubectl -n dora port-forward svc/dora-exporter 9101:9101` | <http://localhost:9101/metrics> |
| **Deploy history** | — | GitHub → repo → **Environments** → `production` |

ArgoCD admin password:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

The DORA dashboard is at Grafana → Dashboards → **DevBoard** folder → **DevBoard
— DORA Metrics** (UID `devboard-dora`).

---

## 1 — CI/CD pipeline

`.github/workflows/devsecops.yml`. Triggers on push and PR to **`feat/full`**.

> This was the single biggest gap found. The pipeline previously triggered on
> `branches: [mega-project]` — a branch that does not exist in this fork — so
> **it had never run once**. Everything downstream of it was dead code.

Gates run first, in parallel. Nothing publishes until all of them pass.

| Job | What it checks | Blocking |
|---|---|---|
| `code-quality` | eslint (frontend), `go fmt` + `go vet` (backend), ruff (ai-service) | yes |
| `code-tests` | `go test`, `npm run test`, `pytest` | yes |
| `secret-scanning` | GitLeaks across full history | yes |
| `dependency-checks` | govulncheck, `npm audit`, pip-audit | reports only |
| `docker-checks` | hadolint + **Trivy CRITICAL** | **yes — CRITICAL blocks** |
| `policy-checks` | Kyverno policies against the rendered chart | yes |
| `sonar-qube` | SonarCloud | reports only |

Then, only on push (never on PR):

| Job | What it does |
|---|---|
| `docker-push` | builds, pushes, **cosign-signs**, generates + attests SBOM, verifies its own signature |
| `gitops-bump` | writes the new tag into `helm/devboard/values.yaml` and `k8s/`, commits `ci: deploy <tag>` |
| `deployment-record` | writes a deployment event to the GitHub Deployments API |

`deployment-record` is what makes DORA measurable. A GitOps pipeline ends with
a commit, and the deploy happens later inside ArgoCD with nothing writing it
down — counting workflow runs counts builds, including ones that never synced.

**Check a run:**

```bash
gh run list --branch feat/full --limit 5
gh run view <run-id>
```

---

## 2 — Trivy gate

`.github/workflows/docker-scans.yml`. Previously ran with
`continue-on-error: true`, so a CRITICAL CVE produced a green tick — scanning,
not gating.

| Step | Severity | Behaviour |
|---|---|---|
| Trivy [CRITICAL — blocking] | CRITICAL | **fails the build** |
| Trivy [HIGH — report only] | HIGH | logged, does not block |
| Trivy [SARIF] | HIGH+CRITICAL | uploaded as an artifact |

`ignore-unfixed: true` — only findings with an available fix can block, so the
gate never asks for something impossible.

**It is currently firing on the frontend image**, on two CVEs in a Go binary
vendored inside `node_modules`:

```
app/node_modules/@esbuild/linux-x64/bin/esbuild (gobinary)
  CVE-2024-24790  CRITICAL  stdlib v1.20.12  fixed in 1.21.11, 1.22.4
  CVE-2025-68121  CRITICAL  stdlib v1.20.12  fixed in 1.24.13, 1.25.7
```

Root cause: `frontend/Dockerfile` copies the entire `node_modules` —
devDependencies included — into the runtime stage, so a build-time tool ships
to production carrying its own vulnerable Go runtime. This is also why the
image is 557MB.

To suppress rather than fix, add to `.trivyignore` with a reason and an expiry:

```
CVE-2024-24790  # esbuild devDependency, not reachable at runtime. expires 2026-11-01
CVE-2025-68121  # esbuild devDependency, not reachable at runtime. expires 2026-11-01
```

---

## 3 — Supply chain: cosign + SBOM

`.github/workflows/docker-push.yml`, per image, in order:

1. build and push → capture the immutable **digest**
2. `cosign sign` — keyless, GitHub Actions OIDC identity, logged to Rekor
3. `syft` SBOM in SPDX JSON → attached with `cosign attest`
4. `cosign verify` against the same constraints Kyverno uses in-cluster

Everything after step 1 addresses the **digest**, never the tag. Signing a tag
signs whatever happens to be sitting there when someone verifies it.

Keyless means no private key exists to leak or rotate.

**Verify by hand** (once the pipeline has published a signed image):

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/manish-jha18/devboard-hackathon/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  manishjha18/devboard-frontend:<tag>

# read the SBOM back out of the registry
cosign download attestation manishjha18/devboard-frontend:<tag> \
  | jq -r .payload | base64 -d | jq '.predicate.packages | length'
```

---

## 4 — Policy as code (Kyverno)

Controller: `gitops/argocd/platform/kyverno.yaml` (chart 3.8.2).
Policies: `gitops/kyverno/policies/`, applied by `kyverno-policies` app.

All four ship in **Audit** mode — they report, they do not block.

| Policy | What it checks | Why |
|---|---|---|
| `verify-image-signatures` | DevBoard images carry a Sigstore signature from this repo's workflow | someone with the registry token can push an image; they cannot forge a Sigstore identity |
| `disallow-latest-tag` | no `:latest`, no untagged images | `:latest` makes "what is running" unanswerable — breaks lead time and rollback at once |
| `require-probes-and-resources` | every container has a readinessProbe and CPU/memory requests | canary analysis cannot judge a rollout whose pods have no readiness signal |
| `require-restricted-security-context` | runAsNonRoot, no privilege escalation, drop ALL capabilities | Pod Security Standards "restricted", trimmed to what these images can satisfy |

Scope: namespaces `devboard` and `devboard-helm` only.
`failurePolicy: Ignore` on all four — if the Kyverno webhook is unreachable the
cluster keeps admitting pods, because a policy engine that takes the cluster
down when it dies is worse than the unsigned image it was meant to catch.

**See the findings:**

```bash
kubectl get clusterpolicy
kubectl get policyreport -n devboard
kubectl get policyreport -n devboard -o wide      # pass/fail per resource

# what exactly failed on one workload
kubectl get policyreport -n devboard -o json \
  | jq -r '.items[].results[] | select(.result=="fail") | "\(.policy)/\(.rule): \(.message)"' \
  | sort -u
```

Current: **36 pass, 39 fail** across 13 reports — real findings on the
hand-applied stack, which predates all of these rules.

The same policies run in CI against the rendered Helm chart, so violations
surface on the PR rather than at admission time.

**Flipping to Enforce** — one at a time, `disallow-latest-tag` first,
`verify-image-signatures` last:

```bash
sed -i 's/failureAction: Audit/failureAction: Enforce/' \
  gitops/kyverno/policies/disallow-latest-tag.yaml
```

---

## 5 — Progressive delivery (Argo Rollouts)

Controller: `gitops/argocd/platform/argo-rollouts.yaml` (chart 2.41.1, app v1.9.1).
Rollout: `helm/devboard/templates/frontend-rollout.yaml`, active when
`frontend.rollout.enabled: true` (the default).

```
setWeight 20 → pause 2m → setWeight 50 → pause 2m → setWeight 100
```

`AnalysisTemplate` runs against Prometheus from the first step:

| Metric | Query source | Fails if |
|---|---|---|
| `gateway-success-rate` | `traces_span_metrics_calls_total{service_name="devboard-gateway.devboard"}` | < 0.95, twice |
| `gateway-latency-p95` | `traces_span_metrics_duration_milliseconds_bucket` | > 1500ms, twice |

On failure the AnalysisRun fails, the rollout **aborts by itself**, and traffic
returns to the previous ReplicaSet — kept warm by
`abortScaleDownDelaySeconds: 30`, so restore is immediate rather than a cold
start. That is the automated part of automated rollback.

**Honest limitation:** this is a *replica-based* canary. No Gateway API
traffic-router plugin is installed, so stable and canary pods share one Service
and traffic splits roughly by pod count. The analysis metrics are therefore
**blended across both versions** — a fully broken canary at `setWeight: 20`
moves the blended success rate to ~80%, which the 0.95 threshold is calibrated
to catch, but it cannot attribute an error to a specific version. Install the
Gateway API plugin and add `spec.strategy.canary.trafficRouting` for per-version
attribution, then tighten the thresholds.

**Watch one:**

```bash
kubectl argo rollouts get rollout devboard-frontend -n devboard-helm --watch
kubectl argo rollouts undo devboard-frontend -n devboard-helm   # manual abort
```

> **Nothing to watch yet** — `kubectl get rollouts -A` returns nothing, because
> the Helm stack has not been deployed. See [Deploying the Helm stack](#deploying-the-helm-stack).

---

## 6 — Rollback and alerting

`.github/workflows/rollback.yml`, two entry points:

- **`workflow_dispatch`** — a human, with a **required reason**. A rollback
  with no recorded reason is a change failure that never gets counted.
- **`repository_dispatch` type `rollback`** — Alertmanager, unattended.

It reverts the last `ci: deploy` commit **with git, not kubectl**. Using kubectl
would leave Git claiming the broken revision is live, and ArgoCD's `selfHeal`
would dutifully re-deploy the failure.

It also writes two records: marks the failed deployment `failure` (change
failure rate numerator) and creates a rollback deployment (MTTR stop-clock).

Alert rules — `gitops/observability/manifests/prometheusrule-dora.yaml`:

| Alert | Fires when | Labels |
|---|---|---|
| `DeployedRevisionFailing` | gateway success rate < 95% for 5m | `action: rollback` |
| `RolloutAborted` | `rollout_phase{phase="Abort"} == 1` | `dora_incident: true` |
| `RolloutError` | `rollout_phase{phase="Error"} == 1` | `dora_incident: true` |
| `RolloutStuckPaused` | paused > 30m — usually an Inconclusive AnalysisRun | — |
| `DoraExporterDown` | exporter not scraped for 10m | — |
| `DoraExporterCannotReachGitHub` | GitHub poll failing 15m | — |
| `NoDeploymentsRecorded` | zero deploys in the window, exporter healthy | — |

> The phase values the controller actually emits are `Abort`, `Completed`,
> `Error`, `Paused`, `Progressing`, `Timeout`. There is **no `Degraded` phase** —
> that is ArgoCD's Application health status. An alert written against
> `phase="Degraded"` matches nothing and never fires. Confirmed by probing the
> live controller.

### Alertmanager is disabled

`gitops/observability/prometheus-values.yaml` sets `alertmanager.enabled: false`,
and there are no Alertmanager pods in the cluster. **Consequence: these rules
are evaluated by Prometheus and will show as firing in its UI, but nothing
routes them anywhere.** The `action: rollback` label has no consumer, so the
alert→rollback loop is not closed.

To close it — this reverses a deliberate setting, so it is left to you:

```yaml
# gitops/observability/prometheus-values.yaml
alertmanager:
  enabled: true
```

Then create the PAT (it cannot live in Git) and add the receiver:

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

Until then `rollback.yml` still runs from the Actions tab, and the deployment
records are written either way — so MTTR stays measurable, just not automatic.

---

## 7 — DORA metrics

`dora-exporter/` — reads the **GitHub Deployments API** and exposes Prometheus
metrics. Deployed by `gitops/argocd/platform/dora-exporter.yaml` into the
`dora` namespace, scraped every 60s.

| Metric | Meaning |
|---|---|
| `dora_deployments_total` | successful deployments in the window |
| `dora_deployment_frequency_per_day` | deployment frequency |
| `dora_lead_time_seconds` | **median** commit-authored → deployed |
| `dora_lead_time_p95_seconds` | 95th percentile lead time |
| `dora_change_failure_rate` | fraction requiring remediation (0–1), `NaN` when no deploys |
| `dora_failed_deployments_total` | deployments marked failed |
| `dora_mttr_seconds` | median failure → rollback |
| `dora_last_deployment_timestamp_seconds` | when the last deploy landed |
| `dora_exporter_last_scrape_success` | 1 = GitHub reachable, 0 = numbers are stale |

Definitions worth being precise about:

- **Lead time** is measured from the commit's *authored* timestamp, not from
  when CI started. Measuring from CI start deletes review latency, which is
  usually most of the number.
- **Change failure rate** counts deployments that *required remediation* — the
  DORA definition. Not alerts, not incidents in general.
- **MTTR** is time-to-restore, not time-to-fix-the-underlying-bug.
- Medians, not means, throughout — one change that sat in review for three
  weeks should not redefine the team's lead time.
- Change failure rate reports **`NaN` → "no data"** when nothing has deployed.
  A green 0% tile over an idle pipeline is the most common way a DORA dashboard
  lies.

**Dashboard panels** — threshold colours encode the Accelerate performance
bands (they are status colours, not series identity):

| Metric | Elite | High | Medium |
|---|---|---|---|
| Deployment frequency | ≥ 1/day | ≥ 1/week | ≥ 1/month |
| Lead time | < 1 day | < 1 week | < 1 month |
| Change failure rate | ≤ 15% | ≤ 30% | ≤ 30% |
| Time to restore | < 1 hour | < 1 day | < 1 week |

**Current values are all 0 / NaN.** That is correct, not broken — no deployment
has been recorded yet, because `docker-push` is blocked by the Trivy gate.

### Optional GitHub token

The exporter works unauthenticated against a public repo at 60 requests/hour,
and spends one call per deployment for its status — so it will start returning
403 once there is real history.

```bash
kubectl -n dora create secret generic dora-github \
  --from-literal=token=<PAT with repo scope>
kubectl -n dora rollout restart deploy/dora-exporter
```

---

## Unblocking the pipeline

`docker-push`, `gitops-bump` and `deployment-record` are all skipped while the
Trivy gate fails, so DORA has nothing to count. Pick one:

1. **Suppress** — add the two esbuild CVEs to `.trivyignore` with an expiry
   (snippet in [section 2](#2--trivy-gate)). Fastest; leaves an audit trail.
2. **Fix properly** — stop copying `node_modules` into the runtime stage of
   `frontend/Dockerfile`; serve `dist/` from a static server. Removes the CVEs
   and roughly 500MB.

---

## Deploying the Helm stack

`gitops/argocd/devboard-helm.yaml` exists but was never applied — which is why
`gitops-bump`'s image bumps have had no consumer and no `Rollout` exists.

It targets namespace **`devboard-helm`**, not `devboard`, so it stands up a
**parallel** stack with its own Gateway and its own load balancer. The
hand-applied stack in `devboard` is untouched.

```bash
kubectl apply -f gitops/argocd/devboard-helm.yaml
kubectl -n devboard-helm get gateway devboard-gateway   # new ELB hostname
kubectl argo rollouts get rollout devboard-frontend -n devboard-helm --watch
```

> **Image coordinates:** `helm/devboard/values.yaml` now points at
> `manishjha18/devboard-*` at tag `sha-4945e5b` — a tag that was built from the
> upstream repo and **does not exist under `manishjha18`**. Deploying before the
> pipeline has published its own will `ImagePullBackOff`. Either run the
> pipeline first (it bumps these in the same commit), or, if you intend to keep
> running the upstream images, set the three `repository:` values back to
> `trainwithshubham/devboard-*`.

---

## Known issues

**`kyverno` app sits OutOfSync / Healthy.** Cosmetic; the policies work. The 11
`policies.kyverno.io` CRDs report drift. Ruled out: deep-diffing chart vs live
(only `/spec/conversion` differs, defaulted by the API server),
`ignoreDifferences` on that pointer, and `RespectIgnoreDifferences=true` —
neither changed the status. The live CRDs carry no labels at all, including
ArgoCD's tracking label, and an explicit sync returns "Succeeded — no more
tasks" while they still gain none. ArgoCD is diffing these CRDs but excluding
them from its sync task list. **Do not "fix" this with `Replace=true`** — that
recreates the CRDs and takes every ClusterPolicy with them.

**Terraform CI fails on a stale lock file.** The committed
`terraform/.terraform.lock.hcl` pins `hashicorp/aws 6.58.0`, but a module
constraint requires `>= 6.59.0`:

```
locked provider registry.terraform.io/hashicorp/aws 6.58.0 does not match
configured version constraint >= 6.0.0, ~> 6.0, >= 6.28.0, >= 6.59.0
```

It passes on the EC2 box only because the working copy has an updated,
uncommitted lock file with 6.60.0. Committing that one file fixes CI.

**`devboard-raw` app is Degraded.** Applied outside this work; not investigated.
