# =============================================================================
# DataShield CMS — production image.
#
# This used to run `npm run dev`: the Vite development server, unminified, with
# a hot-reload websocket held open. It is not built to face the internet and its
# own documentation says so. Now: build once, serve the static output from nginx,
# and reverse-proxy the API from the same origin (see nginx.conf.template for why
# same-origin is a correctness requirement here, not a preference).
#
# For local development with hot reload, use `npm run dev` on the host, or the
# `cms-frontend-dev` service in docker-compose.yml.
# =============================================================================

# --- build -------------------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# Baked into the bundle at build time, so a running container can be traced back
# to the exact commit a customer saw.
ARG VITE_BUILD_SHA=dev
ENV VITE_BUILD_SHA=$VITE_BUILD_SHA

# The banners' publishable key. Safe to bake in — that is the entire point of a
# publishable key, and the security model does not rest on it being hidden.
ARG VITE_PUBLISHABLE_KEY=
ENV VITE_PUBLISHABLE_KEY=$VITE_PUBLISHABLE_KEY

# `npm run build` also runs scripts/check-preview-locks.mjs, which fails the
# build if a preview module has a mutating control that is not disabled. That
# check runs here rather than only in CI on purpose: the image is what reaches a
# customer, so the image is where the guarantee has to hold.
RUN npm run build


# --- serve -------------------------------------------------------------------
FROM nginx:1.27-alpine AS serve

# The entrypoint substitutes ${BACKEND_URL} / ${GATEWAY_URL} into any template
# in this directory at container start, so one image serves every environment.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

COPY --from=build /app/dist /usr/share/nginx/html

# Defaults match the compose service names; Container Apps overrides them with
# the internal FQDNs of the backend and gateway apps.
ENV BACKEND_URL=http://cms-backend:8100 \
    GATEWAY_URL=http://fastapi-gateway:8000

# nginx:alpine ships an unprivileged `nginx` user. Port 8080 (not 80) so binding
# needs no capability: this is the one container exposed to the internet, and it
# has no business running as root to serve static files.
RUN touch /var/run/nginx.pid \
 && chown -R nginx:nginx /var/run/nginx.pid /var/cache/nginx /usr/share/nginx/html /etc/nginx/conf.d
USER nginx

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
