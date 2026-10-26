# Admin packing videos (VMS)

The admin navigation has a **Packing videos** page. Search by the displayed Shopify order number (with or without #) or the numeric Shopify order ID. Select an order, open a webcam/phone camera, start and stop recording, review it, then save. Existing MP4/WebM files and compatible MOV files can also be uploaded.

Inside every return/exchange detail modal, **Packing / opening videos** opens the same order's video library. Original packing is attached to the order even before a request exists. Return opening requires an existing return or exchange request; exchange packing requires an exchange request belonging to that order. All videos for the order are visible together, labeled with their recording type and request number.

Only authenticated admins can search orders, upload, stream or download videos. Customer sessions cannot access these endpoints. Uploads require the existing session CSRF token. Playback supports byte ranges and responses are not cached. Files are outside the photo/static directories; do not add a public nginx alias for them.

## Deploy to the existing VPS

1. Deploy the backend and frontend code together. Back up the database first. From `/var/www/tanoti-returns/backend`, run:

   ```sh
   .venv/bin/python -m flask --app run:app db upgrade
   ```

   The migration creates `order_videos`; it does not change existing requests or orders. The existing deployment script already runs migrations.

2. Set these values in the backend environment file, choosing a private persistent directory writable by your service user:

   ```dotenv
   VIDEOS_STORAGE_DIR=/var/lib/tanoti/videos
   MAX_VIDEO_UPLOAD_MB=200
   MAX_VIDEO_STORAGE_MB=20000
   VIDEO_MIN_FREE_MB=1024
   ```

   Create that directory and grant ownership to the actual systemd service user. The hardened `deploy/tanoti.service` template now sets the video directory under its writable `/var/lib/tanoti` directory. Your existing `tanoti-returns.service` may use a different user/path; check it before applying permissions. Do not put videos inside frontend/dist. The app's fallback path is backend/private_videos.

3. Merge the VMS changes from `deploy/nginx.conf` into the **active** nginx site; do not replace your existing domain, TLS or upstream settings. The template uses port 8000, while the existing deployment checks port 8010. Keep the upstream from your current site.

   - Change the page's Permissions-Policy to `camera=(self), microphone=(), geolocation=()`.
   - Add `media-src 'self' blob:;` to the page's Content-Security-Policy.
   - Add a dedicated `/api/admin/videos` proxy location with `client_max_body_size 201m`, `client_body_timeout 120s`, `proxy_read_timeout 180s`, and `proxy_request_buffering on`. Preserve the proxy headers and request limits used by the existing API location.
   - Keep the existing customer/photo limits in place.
   - If a CDN, tunnel or other proxy has a smaller upload allowance, lower MAX_VIDEO_UPLOAD_MB and the nginx limit to fit it (allow multipart overhead), or configure that proxy appropriately. This implementation uploads each clip in one request; it does not use resumable chunks.

   Run `sudo nginx -t` before reloading nginx. Restart the backend service after environment changes. Build the frontend with `npm ci` and `npm run build` and deploy dist through the existing frontend deployment.

4. Check the live HTTPS site using an admin login: record a short clip, save, reload, play/seek/download it, and open it from a return detail. Test the actual packing camera/phone. Localhost works for development; ordinary HTTP LAN addresses cannot use browser camera recording.

## Storage and retention

This version uses VPS disk only; DO Spaces is not required and is not wired into these endpoints. Increasing disk storage or mounting a persistent volume at VIDEOS_STORAGE_DIR works. Keep a separate backup of the directory **and database**, and check restored playback. Backups consume additional space.

Default video quota is 20,000 MiB, with at least 1,024 MiB free required on the video filesystem. New saves are rejected when a limit is reached; existing evidence is never automatically deleted. Choose a larger quota only after checking actual free disk space. Monitor nginx's upload-buffer disk and the app's temporary filesystem too; they briefly hold uploads before the final copy. Failed uploads retain the browser's recording for retry or download while the page remains open. A page close, browser crash or device sleep can still lose an unsaved recording.

At 2 Mbps, a two-minute clip is about 30 MB. At 30 orders/day plus opening and exchange-packing clips for 20% of orders, budget approximately 38 GB/month (114 GB for 90 days), excluding backups and temporary files. This is a planning estimate: real duration and browser bitrate vary. The 20,000 MiB default is a pilot cap, not 90-day capacity. Retention/deletion must be managed deliberately; no automated retention or video-delete UI is included.

Camera recording requests 720p at 24 fps and 2 Mbps, without audio. It stops after 10 minutes or near the configured upload size, whichever comes first. Uploaded files are not transcoded, and their original audio is retained. Container signatures are checked but videos are not fully decoded/validated server-side. Playback depends on the browser supporting the file's codecs; download unsupported clips for an external player. File hashes and the saving admin/time are stored for traceability; this is not tamper-proof forensic certification.

## Validation

Backend tests cover admin/customer authorization, CSRF, order/request association, all recording types, authenticated range playback, upload limits, quota/free-space failures, failed-save cleanup and idempotent retries. Migration tests check the new table along with preservation of existing records. A real camera and the production nginx/service configuration must still be checked after deployment.
