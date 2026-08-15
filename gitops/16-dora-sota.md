# 16 — DORA and progressive delivery

This chapter closes the loop between the pipeline and the cluster: canary
deploys that roll themselves back, policies that run in CI and at admission,
images that can prove where they came from, and four numbers that say whether
any of it is working.

Everything here is additive. Nothing in chapters 01–15 changes behaviour.

---

## What was wrong first

Three things had to be fixed before any DORA metric could mean anything.

**The pipeline never ran.** `devsecops.yml` triggered on `branches:
[mega-project]`, a branch that does not exist in this fork. Every gate, build
and image push in chapters 01–15 was dormant. Retargeted to `feat/full`.

**Nothing consumed the pipeline's output.** `gitops-bump` writes the new image
tag into `helm/devboard/values.yaml` and `k8s/`, expecting ArgoCD to notice.
But the running stack was applied by hand with `kubectl apply -f k8s/` — no
`argocd.argoproj.io/instance` label, no Application watching it. CI could have
pushed images forever without deploying one. See "Turning on GitOps delivery"
below.

**Every gate was advisory.** Trivy, Checkov and the IaC scan all ran with
`continue-on-error: true`, so a CRITICAL CVE produced a green tick. Scanning
without gating is a dashboard, not a control. The Trivy CRITICAL step is now
blocking, with `.trivyignore` as the auditable escape hatch.

---

## Deployment frequency and lead time

DORA needs a *deployment event*. GitOps does not naturally produce one: CI ends
with a commit, and the deploy happens minutes later inside ArgoCD with nothing
writing it down. Counting workflow runs counts builds — including the ones that
never synced.

So `deployment-record.yml` writes an explicit record to the **GitHub Deployments
API** after `gitops-bump` succeeds, carrying the image tag and the commit's
*authored* timestamp. `dora-exporter` reads it back.

Lead time is measured from when the code was committed, not when CI started.
Measuring from CI start deletes review latency, which is usually most of the
number.

Deploy history is visible in three places without kubectl:

| Where | Shows |
|---|---|
| GitHub → Environments → production | every deploy, its commit, success/failure |
| Argo Rollouts dashboard | live revision, canary progress, last N ReplicaSets |
| Grafana → DevBoard → DORA Metrics | the aggregate four keys |

---

## Progressive delivery

`helm/devboard/templates/frontend-rollout.yaml` replaces the frontend
Deployment with an Argo Rollout when `frontend.rollout.enabled` is true:

```
setWeight 20 → pause 2m → setWeight 50 → pause 2m → setWeight 100
```

An `AnalysisTemplate` runs against Prometheus from the first step. If gateway
success rate drops below 95%, or p95 latency exceeds 1500 ms, twice, the
AnalysisRun fails and the rollout **aborts on its own** — traffic returns to the
previous ReplicaSet, which `abortScaleDownDelaySeconds: 30` keeps warm so the
restore is immediate rather than a cold start.

### The honest limitation

This is a **replica-based** canary. There is no Gateway API traffic-router
plugin installed, so stable and canary pods sit behind one Service and traffic
splits roughly by pod count. The consequence: the analysis metrics are
**blended** across both versions. A canary serving nothing but errors at
`setWeight: 20` moves the blended success rate to about 80%, which is what the
0.95 threshold is calibrated to catch — but it cannot attribute an error to a
specific version.

To get per-version attribution, install the Argo Rollouts Gateway API plugin and
add `spec.strategy.canary.trafficRouting`. The thresholds can then tighten
considerably, because the query can filter on the canary Service alone.

---

## Policy as code

Four ClusterPolicies in `gitops/kyverno/policies/`, all shipped in **Audit**
mode:

| Policy | Why it exists |
|---|---|
| `verify-image-signatures` | an attacker with the Docker Hub token can push an image; they cannot forge a Sigstore identity |
| `disallow-latest-tag` | `:latest` makes "what is running" unanswerable, breaking lead time and rollback at once |
| `require-probes-and-resources` | canary analysis cannot judge a rollout whose pods have no readiness probe |
| `require-restricted-security-context` | non-root, no privilege escalation, drop ALL |

The same rules run in CI (`policy-check.yml`) against the rendered chart, so
violations surface on the PR rather than at admission time. `kyverno apply`
exits 0 for Audit-mode policies — correct for the cluster, wrong for CI — so
the workflow parses the report and fails on any `fail:` count itself.

### Flipping to Enforce

Audit is the starting position because everything currently running predates
signing, and Enforce would wedge the cluster on the next pod restart. Once a
signed tag has rolled all the way through:

```bash
# check what would break first
kubectl get policyreport -A -o wide

# then, per policy
sed -i 's/failureAction: Audit/failureAction: Enforce/' \
  gitops/kyverno/policies/<policy>.yaml
```

Do them one at a time, starting with `disallow-latest-tag`. Leave
`verify-image-signatures` for last — it is the one that can stop a rollback
from deploying if the artifact you are rolling back to predates signing.

Note `failurePolicy: Ignore` on every policy: if the Kyverno webhook is
unreachable the cluster keeps admitting pods. A policy engine that takes the
cluster down when it dies is a worse availability risk than the unsigned image
it was meant to catch.

---

## Supply chain

`docker-push.yml` now, per image:

1. builds and pushes, capturing the immutable **digest**
2. `cosign sign` — keyless, using the workflow's OIDC identity, logged to Rekor
3. `syft` SBOM in SPDX JSON, attached as a signed attestation with `cosign attest`
4. `cosign verify` against the same constraints Kyverno uses in-cluster

Everything after step 1 addresses the digest, never the tag. Signing a tag
signs whatever happens to be sitting there when someone verifies it.

Keyless means there is no private key to leak, rotate, or forget to rotate. The
trade is a dependency on Sigstore's public good infrastructure being reachable
from CI and from the Kyverno controller.

Verify by hand:

```bash
cosign verify \
  --certificate-identity-regexp '^https://github.com/manish-jha18/devboard-hackathon/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  manishjha18/devboard-frontend:<tag>

cosign download attestation manishjha18/devboard-frontend:<tag> \
  | jq -r .payload | base64 -d | jq .predicate.name
```

---

## Change failure rate and MTTR

Change failure rate counts deployments that **required remediation** — the DORA
definition. Not alerts, not incidents in general. `rollback.yml` marks the
deployment it reverted as `failure`, which is what makes the number real rather
than a guess.

MTTR is measured from that failure status to the rollback deployment that
resolved it: restore time, not time-to-fix-the-underlying-bug.

### Wiring the alert to the rollback

`DeployedRevisionFailing` in `prometheusrule-dora.yaml` carries
`action: rollback`. To close the loop automatically, add an Alertmanager
receiver that POSTs a `repository_dispatch` to GitHub. The PAT cannot live in
Git, so create it out of band:

```bash
kubectl -n observability create secret generic alertmanager-github \
  --from-literal=token=<PAT with repo scope>
```

Then add to the Alertmanager config in `gitops/observability/prometheus-values.yaml`:

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

Until that is wired, `rollback.yml` still runs from the Actions tab with a
required reason — the record is created either way, so MTTR stays measurable.

**Roll forward, do not revert the revert.** The workflow reverts the last
`ci: deploy` commit specifically; anything else touching those paths was a
human, and reverting a human's commit unannounced is not its job.

---

## The dashboard

Grafana → DevBoard folder → **DORA Metrics**, or by UID `devboard-dora`.

Threshold colours encode the Accelerate performance bands, not series identity:

| Metric | Elite | High | Medium |
|---|---|---|---|
| Deployment frequency | ≥ 1/day | ≥ 1/week | ≥ 1/month |
| Lead time | < 1 day | < 1 week | < 1 month |
| Change failure rate | ≤ 15% | ≤ 30% | ≤ 30% |
| Time to restore | < 1 hour | < 1 day | < 1 week |

Change failure rate deliberately renders **"no data"** rather than 0% when
nothing has deployed. A green tile over an idle pipeline is the most common way
a DORA dashboard lies.

### The exporter's GitHub token

Optional. Unauthenticated works against a public repo at 60 requests/hour, and
each refresh spends one call per deployment for its status — so it will start
returning 403 once there is real history. Fix:

```bash
kubectl -n dora create secret generic dora-github \
  --from-literal=token=<PAT with repo scope>
kubectl -n dora rollout restart deploy/dora-exporter
```

---

## Turning on GitOps delivery

The Helm stack has an Application that was never applied:
`gitops/argocd/devboard-helm.yaml`. It targets namespace **`devboard-helm`**,
not `devboard` — so it stands up a parallel stack with its own Gateway and its
own load balancer. The hand-applied stack in `devboard` is untouched.

```bash
kubectl apply -f gitops/argocd/devboard-helm.yaml
kubectl -n devboard-helm get gateway devboard-gateway   # new ELB hostname
```

**Run the pipeline at least once first.** The image tags committed in
`values.yaml` were built from the upstream repository and do not exist under
`manishjha18`; deploying before CI has published its own will
`ImagePullBackOff`. The first successful run bumps them in the same commit.

Once the new stack is verified, point DNS at it and delete the hand-applied one:

```bash
kubectl delete -f k8s/
```

Until that cutover happens, deployment frequency measures what CI *recorded*,
not what is serving traffic on the original load balancer.
