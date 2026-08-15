FROM dhi.io/node:26-dev AS build

WORKDIR /app

COPY package*.json ./

RUN npm install --legacy-peer-deps

RUN npm list vite > /dev/null 2>&1 || npm install --no-save vite@5.4.11

COPY . .

RUN npm run build

# The runtime stage ships ONLY the built assets and an nginx config.
#
# It used to be dhi.io/node:26 running `vite preview`, which meant copying the
# entire node_modules tree — devDependencies and all — into the production
# image. Two consequences, one root cause:
#
#   - esbuild's Go binary rode along, carrying CVE-2024-24790 and
#     CVE-2025-68121 from a Go stdlib nothing in production ever executes.
#     Those are CRITICAL and fixed upstream, so the Trivy gate in
#     .github/workflows/docker-scans.yml blocked the build on them.
#   - the image was 557MB to serve about 290KB of static assets.
#
# Serving the build output directly fixes both. nginx.conf reproduces the two
# things vite preview was actually doing: SPA fallback, and proxying /api to
# the backend.
FROM dhi.io/nginx:1

COPY --from=build /app/dist /usr/share/nginx/html

COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 4173

# No ENTRYPOINT or CMD: the base image already runs `nginx -g "daemon off;"` as
# UID 65532. Restating them here would just create a second place to keep in
# sync with the upstream image.
