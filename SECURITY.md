# Security

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Email the maintainer (see the GitHub profile of the repository owner) with a description, reproduction steps and the affected version. You will get an acknowledgement within a few days; fixes are released as patch versions and credited in the changelog unless you prefer otherwise.

## Deployment model and what AudioFP does (and does not) protect

AudioFP is designed to run **inside a trusted network** or behind a reverse proxy that handles TLS, authentication and rate limiting. Out of the box:

- All endpoints are unauthenticated unless `AUDIOFP_API_KEY` is set. Set it for anything reachable by other people; the key is compared in constant time.
- `POST /api/v1/tracks/index-directory` makes the server read files from its own filesystem. In the `production` profile it only works inside the folders listed in `AUDIOFP_INDEX_ROOTS`; set `AUDIOFP_ALLOW_DIRECTORY_INDEXING=false` to disable it entirely. In `development` any path is allowed — do not expose a development server.
- Audio streaming (`/tracks/<id>/audio`) only serves files that were indexed, never arbitrary paths. Deleting files through the API is limited to the upload folder.
- Uploaded file names are sanitised and stored under random prefixes; uploads are size-limited (`AUDIOFP_MAX_UPLOAD_MB`).
- Error responses never include stack traces or server paths; a `request_id` links them to the server log.
- The UI stores the API key in the browser's `localStorage`. Use HTTPS (via your proxy) so it is not sent in clear text.
- ffmpeg, when installed, decodes untrusted input. Keep it updated; run the container image (non-root user) if you process files from unknown sources.

Not provided: per-user accounts, rate limiting, audit trails, encryption at rest. Put AudioFP behind your gateway of choice for those.

## Supported versions

| Version | Supported |
|---|---|
| 2.x | yes |
| 1.x | no — upgrade (fingerprints must be rebuilt) |
