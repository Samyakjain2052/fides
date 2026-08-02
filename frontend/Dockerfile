# =============================================================================
# DataShield CMS — the DPDP consent-management frontend.
#
# Dev-server image: Vite with hot reload, which is what you want while building
# screens. For a production build use `npm run build` and serve dist/ from any
# static host (or add an nginx stage here).
# =============================================================================
FROM node:20-alpine

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

EXPOSE 5173

# --host binds outside the container. GATEWAY_URL points the /gateway proxy at
# the DSAR backend (see vite.config.js).
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
