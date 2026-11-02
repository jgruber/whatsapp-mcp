# Deploying to compute3 (Portainer stack)

Two containers, one shared volume. Images are published to Docker Hub as
`jgruber/whatsapp-mcp-bridge` and `jgruber/whatsapp-mcp-server`, and the
Portainer stack pulls from there. The GitHub Actions workflow in
`.github/workflows/docker-publish.yml` rebuilds and pushes both images on every
push to `main` and on tagged releases (`v*`). To push manually, run
`scripts/push-images.sh` (expects you to be logged into Docker Hub).

## Prerequisites

- A Portainer API token with write access to compute3 (endpoint id `8`).
- A `MCP_AUTH_TOKEN` of at least 32 random chars for public access to `/mcp`
  and `/upload`.
- The images tagged with the SHA you want deployed must exist on Docker Hub.
  Confirm with `docker manifest inspect jgruber/whatsapp-mcp-bridge:<SHA>`.

## First-time cutover (side-by-side with the legacy stack)

The existing stack name is `whatsapp-mcp`. Deploy this one as **`whatsapp-mcp-v2`**
on a different host port (`8801` by default) so both stacks run at once. Cut
`whats.grubernet.org` over only after you've confirmed the new stack is paired,
exercises the five group tools, and your known consumers (CLMReminders) still work.

1. **Snapshot the legacy volume.** From compute3:

   ```bash
   docker run --rm -v whatsapp-mcp_store:/src -v /tmp:/dst alpine \
     tar -cf /dst/whatsapp-mcp_store.$(date +%F).tar -C /src .
   ```

   Copy that tarball off compute3. This is the rollback.

2. **Clone a copy of the volume for v2.** The new stack must not share state with
   the old one until you're ready to cut over:

   ```bash
   docker volume create whatsapp-mcp-v2_store
   docker run --rm \
     -v whatsapp-mcp_store:/src:ro \
     -v whatsapp-mcp-v2_store:/dst \
     alpine sh -c 'cp -a /src/. /dst/'
   ```

3. **Publish the images** (CI does this automatically on push to `main`; to do
   it manually from your workstation):

   ```bash
   scripts/push-images.sh
   ```

   Note the short SHA it prints — that is the `IMAGE_TAG` for step 4.

4. **Create the Portainer stack** from `docker-compose.portainer.yml`:

   - Portainer → Stacks → Add stack → Web editor.
   - Name: `whatsapp-mcp-v2`.
   - Paste `docker-compose.portainer.yml`.
   - Environment variables:
     - `IMAGE_TAG` = the short SHA you built.
     - `MCP_AUTH_TOKEN` = long random string ≥ 32 chars.
     - `MCP_HOST_PORT` = `8801` (not `8800`, which the legacy stack holds).
     - `WHATSAPP_DEVICE_NAME` = `whatsapp-mcp-v2`.
   - Volumes tab: rename `store` so Portainer binds to the pre-populated
     `whatsapp-mcp-v2_store` instead of auto-creating a new one. In practice:
     deploy once, then stop the stack, swap the volume with
     `docker service update` or edit the compose to add an explicit
     `external: true` on the volume.
   - Deploy.

5. **Verify.** From compute3:

   ```bash
   curl -fsS http://127.0.0.1:8801/healthz
   curl -fsS -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
     -X POST http://127.0.0.1:8801/mcp \
     -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","method":"tools/list","id":1}' | head
   ```

   `tools/list` should include `send_message`, `send_file`, `list_groups`,
   `get_group_info`, `create_group`, `update_group_participants`,
   `get_group_invite_link`, among others.

   Then open the bridge logs in Portainer and confirm it reports a paired
   session (no QR code) — the cloned volume carries the paired-device DB.

6. **Cut over the public URL.** Point `whats.grubernet.org` at `:8801` (edit
   whatever proxies it today — Caddy/Traefik/nginx on gateway0), redeploy the
   reverse proxy, and watch the next real send succeed. The legacy stack can
   stay running for a day as hot rollback, then be removed.

## Rollback

If anything misbehaves, point the proxy back at `:8800` (the legacy stack).
No data to restore unless v2 wrote to its own volume — which only happens
once a message is received during the test window. If it did, replace the
`whatsapp-mcp-v2_store` with the tarball from step 1 before redeploying.

## Updating the stack

Push to `main` to trigger the GitHub Actions workflow, or run
`scripts/push-images.sh` locally. Then in Portainer → the stack → Editor:
bump `IMAGE_TAG` to the new short SHA and Deploy. The named volume survives
redeploys, so the paired-device session persists.
