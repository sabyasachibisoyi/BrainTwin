# syntax=docker/dockerfile:1.6
#
# BrainTwin Caddy image — Caddy + cloudflare-dns plugin via xcaddy.
#
# Phase 4.0.6 M.4.a. Caddy doesn't ship the Cloudflare DNS plugin in
# its standard image, so we build a custom one. We need the plugin
# because:
#
#   * ACME HTTP-01 challenge doesn't reliably work behind a Cloudflare
#     proxy (Cloudflare can cache /.well-known/acme-challenge/* and
#     break the challenge mid-flight).
#   * ACME DNS-01 challenge sidesteps that — Let's Encrypt asks for a
#     TXT record under _acme-challenge.braintwin.net, Caddy uses the
#     Cloudflare API to publish the record, Let's Encrypt verifies,
#     Caddy retracts the record. All cert issuance happens via DNS,
#     never through HTTP — proxied or not.
#
# Why custom-build vs. a community image
# --------------------------------------
# Caddy with plugins is always a custom build (it's a Go binary with
# plugins compiled in at build time). Community images like
# slothcroissant/caddy-cloudflaredns work, but for an edge service
# that terminates TLS we'd rather not depend on a third party's
# refresh cadence and supply chain. Building our own + pushing to our
# private ECR keeps the trust chain to "what we wrote + what xcaddy
# resolves at build time." See design doc §3.4.
#
# Tag scheme
# ----------
# build-and-push-caddy.sh writes tags as:
#   caddy-<upstream-version>-snapshot-<git-sha>   (pre-launch)
#   caddy-<upstream-version>-v<MAJOR>.<MINOR>.<PATCH>  (release)
# So the operator can tell "what Caddy version" and "what BrainTwin
# build" from a single tag.

# -----------------------------------------------------------------
# Stage 1 — xcaddy builder
# -----------------------------------------------------------------
# caddy:builder-alpine ships xcaddy + the Go toolchain pre-configured
# for the matching Caddy version. The output is a single Caddy binary
# with the requested plugin compiled in.
#
# Pinned to a specific version so the build is reproducible. Bump
# this in sync with the runtime image below when picking up a new
# Caddy upstream. WHEN BUMPING — check that the upstream builder
# image's bundled Go toolchain is >= what the cloudflare-dns plugin
# requires. v0.2.4 of caddy-dns/cloudflare needs Go 1.23+, and the
# Caddy 2.9.x series is the first where the builder image ships
# that. Older Caddy builder images (2.7.x = Go 1.21) fail with
# "requires go >= 1.23.0 (running go 1.21.x; GOTOOLCHAIN=local)".
ARG CADDY_VERSION=2.9.1
FROM caddy:${CADDY_VERSION}-builder-alpine AS builder

# GOTOOLCHAIN=auto lets the Go toolchain auto-download a newer Go if
# a module's go.mod asks for one beyond what's installed. Defensive:
# if a future plugin bumps its Go requirement before we bump
# CADDY_VERSION here, the build still works rather than failing with
# the same regex error this Dockerfile just dodged.
ENV GOTOOLCHAIN=auto

# caddy-dns/cloudflare is the maintained DNS provider plugin used by
# Caddy's ACME client for the DNS-01 challenge. The plugin is small
# (~100 KB) and adds only Cloudflare-API code paths to the binary.
RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare

# -----------------------------------------------------------------
# Stage 2 — runtime image
# -----------------------------------------------------------------
# Use the same upstream version so the runtime configuration (default
# Caddyfile location, container entrypoint, etc.) matches what the
# custom binary expects.
FROM caddy:${CADDY_VERSION}-alpine

# Re-declare the build ARG inside this stage so the LABEL below can
# interpolate it. Global ARGs (declared before the first FROM) are
# only available in FROM lines unless re-declared per stage. Without
# this, ${CADDY_VERSION} in the LABEL would render as a literal
# string, which makes `docker inspect` less useful.
ARG CADDY_VERSION

# Swap the stock /usr/bin/caddy with our custom-built binary. The
# Alpine base image already has the right user, ca-certificates, and
# the default Caddyfile location (/etc/caddy/Caddyfile) configured.
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# Provenance label so `docker inspect` says where this came from.
LABEL org.opencontainers.image.source="https://github.com/sabyasachibisoyi/BrainTwin"
LABEL org.opencontainers.image.description="Caddy ${CADDY_VERSION} + caddy-dns/cloudflare for BrainTwin edge TLS"

# Caddy listens on :80 (redirect) and :443 (TLS). Cloudflare proxy
# fronts both — origin traffic from Cloudflare's IPs only (security
# group rule pins this; AOP validates it cryptographically).
EXPOSE 80 443

# Default entrypoint from upstream image:
#   ENTRYPOINT ["caddy"]
#   CMD ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
# We rely on the upstream defaults — the docker-compose service block
# in compute.ts mounts a Caddyfile at /etc/caddy/Caddyfile.
