# Public python base, deliberately — unlike the three application images this
# one does not use dhi.io. The DHI subscription is a hard dependency for
# anyone who forks this repo, and a metrics exporter is not worth making the
# DORA dashboard unbuildable for them. Swap to dhi.io/python:3.13 if you want
# it on the same hardened base as the rest.
FROM python:3.13-slim AS build

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim

WORKDIR /app

COPY --from=build /install /usr/local
COPY app ./app

# Non-root, so the workload satisfies the same Kyverno restricted policy it is
# reporting on. An exporter that violates the policies it measures is a bad
# look on a compliance dashboard.
RUN useradd --uid 65532 --create-home --shell /usr/sbin/nologin nonroot
USER 65532

EXPOSE 9101

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "app.main"]
