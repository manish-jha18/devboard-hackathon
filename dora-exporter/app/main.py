"""DORA metrics exporter.

Turns the GitHub Deployments API into the four DORA metrics, exposed in
Prometheus format so Grafana can render them next to the cluster's own
telemetry.

Why the Deployments API is the source of truth
----------------------------------------------
In a GitOps pipeline nothing naturally records "a deployment happened". CI
finishes by writing a commit; ArgoCD applies it minutes later on its own
schedule. Counting workflow runs counts builds, not deploys, and counts them
even when the sync fails. So .github/workflows/deployment-record.yml writes an
explicit deployment event, and this reads it back. One event per deploy, with
the originating commit attached — which is what makes lead time computable.

The four metrics, and the honest definition of each
---------------------------------------------------
deployment frequency  count of successful deployments / window in days
lead time for changes deploy time minus the COMMIT AUTHORED time, median.
                      Measuring from CI start instead would delete review
                      latency, which is usually most of the number.
change failure rate   deployments that later received a 'failure' status,
                      over total deployments. A rollback marks the deployment
                      it replaced as failed (see rollback.yml), so this counts
                      changes that required remediation, per the DORA
                      definition — not alerts, not incidents in general.
MTTR                  median time from a deployment being marked failed to the
                      rollback deployment that resolved it. Restore time, not
                      "time to repair the underlying bug".

Percentiles are medians rather than means throughout: one pathological deploy
that sat in review for three weeks should not redefine the team's lead time.
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from prometheus_client import Gauge, start_http_server

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("dora-exporter")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")
REPO = os.getenv("GITHUB_REPOSITORY", "manish-jha18/devboard-hackathon")
TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
ENVIRONMENT = os.getenv("DORA_ENVIRONMENT", "production")
WINDOW_DAYS = int(os.getenv("DORA_WINDOW_DAYS", "30"))
REFRESH_SECONDS = int(os.getenv("DORA_REFRESH_SECONDS", "300"))
PORT = int(os.getenv("PORT", "9101"))
# The API pages at 100. Two pages is plenty for a 30-day window on a project
# deploying a few times a day, and bounds the damage if the window is widened
# carelessly.
MAX_PAGES = int(os.getenv("DORA_MAX_PAGES", "3"))

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

LABELS = ["environment", "repository"]

m_deploy_total = Gauge(
    "dora_deployments_total",
    "Successful deployments observed in the rolling window",
    LABELS,
)
m_deploy_freq = Gauge(
    "dora_deployment_frequency_per_day",
    "Deployment frequency: successful deployments per day over the window",
    LABELS,
)
m_lead_time = Gauge(
    "dora_lead_time_seconds",
    "Lead time for changes: median seconds from commit authored to deployed",
    LABELS,
)
m_lead_time_p95 = Gauge(
    "dora_lead_time_p95_seconds",
    "Lead time for changes, 95th percentile",
    LABELS,
)
m_cfr = Gauge(
    "dora_change_failure_rate",
    "Change failure rate: fraction of deployments that required remediation (0-1)",
    LABELS,
)
m_failed_total = Gauge(
    "dora_failed_deployments_total",
    "Deployments in the window that were marked failed",
    LABELS,
)
m_mttr = Gauge(
    "dora_mttr_seconds",
    "Mean time to restore: median seconds from deployment failure to rollback",
    LABELS,
)
m_last_deploy = Gauge(
    "dora_last_deployment_timestamp_seconds",
    "Unix timestamp of the most recent successful deployment",
    LABELS,
)
m_window_days = Gauge(
    "dora_window_days",
    "Length of the rolling window these metrics are computed over",
    LABELS,
)
m_scrape_ok = Gauge(
    "dora_exporter_last_scrape_success",
    "1 if the last GitHub poll succeeded, 0 otherwise",
    LABELS,
)
m_scrape_ts = Gauge(
    "dora_exporter_last_scrape_timestamp_seconds",
    "Unix timestamp of the last successful GitHub poll",
    LABELS,
)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@dataclass
class Deployment:
    id: int
    created_at: datetime
    committed_at: datetime | None
    is_rollback: bool
    state: str | None = None
    state_at: datetime | None = None

    @property
    def lead_time_seconds(self) -> float | None:
        if self.committed_at is None:
            return None
        delta = (self.created_at - self.committed_at).total_seconds()
        # Clock skew between the runner and GitHub can produce a small
        # negative; a large negative means the payload is wrong and should not
        # be silently folded into the median.
        return delta if delta >= 0 else None


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "devboard-dora-exporter",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("could not parse timestamp %r", value)
        return None


def fetch_deployments(cutoff: datetime) -> list[Deployment]:
    """Deployments for the configured environment, newest first."""
    out: list[Deployment] = []
    session = requests.Session()

    for page in range(1, MAX_PAGES + 1):
        resp = session.get(
            f"{GITHUB_API}/repos/{REPO}/deployments",
            headers=_headers(),
            params={"environment": ENVIRONMENT, "per_page": 100, "page": page},
            timeout=20,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        stop = False
        for item in batch:
            created = _parse_ts(item.get("created_at"))
            if created is None:
                continue
            if created < cutoff:
                # The API returns newest first, so the first item older than
                # the window means every later item is too.
                stop = True
                break

            payload = item.get("payload") or {}
            if isinstance(payload, str):
                payload = {}

            out.append(
                Deployment(
                    id=item["id"],
                    created_at=created,
                    committed_at=_parse_ts(payload.get("committed_at")),
                    is_rollback=bool(payload.get("rollback", False)),
                )
            )

        if stop or len(batch) < 100:
            break

    # Statuses are a separate call per deployment. Bounded by the window, and
    # only needed to distinguish success from failure.
    for dep in out:
        try:
            resp = session.get(
                f"{GITHUB_API}/repos/{REPO}/deployments/{dep.id}/statuses",
                headers=_headers(),
                params={"per_page": 10},
                timeout=20,
            )
            resp.raise_for_status()
            statuses = resp.json()
            if statuses:
                # Newest first; the latest state is the one that counts.
                dep.state = statuses[0].get("state")
                dep.state_at = _parse_ts(statuses[0].get("created_at"))
        except requests.RequestException as exc:
            log.warning("status fetch failed for deployment %s: %s", dep.id, exc)

    return out


# ---------------------------------------------------------------------------
# DORA computation
# ---------------------------------------------------------------------------


def compute_and_publish(deployments: list[Deployment]) -> None:
    labels = {"environment": ENVIRONMENT, "repository": REPO}

    rollbacks = [d for d in deployments if d.is_rollback]
    real_deploys = [d for d in deployments if not d.is_rollback]
    failed = [d for d in real_deploys if d.state == "failure"]
    succeeded = [d for d in real_deploys if d.state == "success"]

    # --- deployment frequency ------------------------------------------
    m_deploy_total.labels(**labels).set(len(succeeded))
    m_deploy_freq.labels(**labels).set(len(succeeded) / WINDOW_DAYS if WINDOW_DAYS else 0.0)

    if succeeded:
        m_last_deploy.labels(**labels).set(
            max(d.created_at for d in succeeded).timestamp()
        )

    # --- lead time -------------------------------------------------------
    lead_times = [
        lt for lt in (d.lead_time_seconds for d in real_deploys) if lt is not None
    ]
    if lead_times:
        m_lead_time.labels(**labels).set(statistics.median(lead_times))
        # quantiles() needs at least two points; below that the max IS the p95.
        if len(lead_times) >= 2:
            p95 = statistics.quantiles(lead_times, n=20)[-1]
        else:
            p95 = lead_times[0]
        m_lead_time_p95.labels(**labels).set(p95)
    else:
        m_lead_time.labels(**labels).set(0)
        m_lead_time_p95.labels(**labels).set(0)

    # --- change failure rate ---------------------------------------------
    m_failed_total.labels(**labels).set(len(failed))
    if real_deploys:
        m_cfr.labels(**labels).set(len(failed) / len(real_deploys))
    else:
        # No deploys is not a 0% failure rate; it is no data. Publishing 0
        # would paint a green tile over an idle pipeline.
        m_cfr.labels(**labels).set(float("nan"))

    # --- MTTR ------------------------------------------------------------
    # Pair each failure with the first rollback recorded after it. Rollbacks
    # are the restore action in this pipeline, so their timestamp is when
    # service was restored.
    restore_times: list[float] = []
    for fail in failed:
        failed_at = fail.state_at or fail.created_at
        later = [r for r in rollbacks if r.created_at >= failed_at]
        if later:
            restore = min(later, key=lambda r: r.created_at)
            restore_times.append((restore.created_at - failed_at).total_seconds())

    if restore_times:
        m_mttr.labels(**labels).set(statistics.median(restore_times))
    else:
        m_mttr.labels(**labels).set(float("nan"))

    m_window_days.labels(**labels).set(WINDOW_DAYS)

    log.info(
        "deploys=%d failed=%d rollbacks=%d lead_time_median=%.0fs",
        len(succeeded),
        len(failed),
        len(rollbacks),
        statistics.median(lead_times) if lead_times else 0,
    )


def refresh_loop() -> None:
    labels = {"environment": ENVIRONMENT, "repository": REPO}
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
            deployments = fetch_deployments(cutoff)
            compute_and_publish(deployments)
            m_scrape_ok.labels(**labels).set(1)
            m_scrape_ts.labels(**labels).set(time.time())
        except Exception as exc:  # noqa: BLE001 — a poll failure must not kill the loop
            log.error("refresh failed: %s", exc)
            m_scrape_ok.labels(**labels).set(0)
        time.sleep(REFRESH_SECONDS)


def main() -> None:
    if not TOKEN:
        # Works unauthenticated against a public repo, but 60 requests/hour is
        # tight once status lookups are counted. Say so rather than letting it
        # fail mysteriously at 03:00.
        log.warning(
            "no GITHUB_TOKEN set — falling back to unauthenticated API access "
            "(60 req/hour). Set one if polling starts returning 403."
        )

    log.info(
        "starting: repo=%s environment=%s window=%dd refresh=%ds port=%d",
        REPO,
        ENVIRONMENT,
        WINDOW_DAYS,
        REFRESH_SECONDS,
        PORT,
    )

    start_http_server(PORT)
    thread = threading.Thread(target=refresh_loop, daemon=True)
    thread.start()
    # start_http_server backgrounds itself, so hold the main thread open.
    thread.join()


if __name__ == "__main__":
    main()
