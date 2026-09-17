# Security

## Reporting a vulnerability

Please don't open a public issue for security problems. Email the maintainer (the address is on the GitHub profile of the repository owner) with a description, steps to reproduce and the affected version. You'll get an acknowledgement within a few days. Fixes go out as patch releases and are credited in the changelog unless you'd rather not be named.

## What AudioFP protects and what it leaves to you

AudioFP is meant to run inside a trusted network, or behind a reverse proxy that takes care of TLS, authentication and rate limiting. The defaults look like this:

- Every endpoint is open unless `AUDIOFP_API_KEY` is set. Set it for anything other people can reach. The key is compared in constant time.
- `POST /api/v1/tracks/index-directory` makes the server read files from its own filesystem. In the `production` profile it only works inside the folders listed in `AUDIOFP_INDEX_ROOTS`, and `AUDIOFP_ALLOW_DIRECTORY_INDEXING=false` switches it off completely. In `development` any path is allowed, so never expose a development server.
- Audio streaming (`/tracks/<id>/audio`) only serves files that were indexed, never arbitrary paths. Deleting files through the API is limited to the upload folder.
- Uploaded file names are sanitised and stored under random prefixes, and uploads are size-limited by `AUDIOFP_MAX_UPLOAD_MB`.
- Error responses never include stack traces or server paths. A `request_id` links them to the server log.
- The UI keeps the API key in the browser's `localStorage`. Put HTTPS in front (through your proxy) so the key isn't sent in clear text.
- When ffmpeg is installed it decodes untrusted input. Keep it updated, and if you process files from unknown sources use the container image, which runs as a non-root user.

There are no per-user accounts, no rate limiting, no audit trail and no encryption at rest. Put AudioFP behind whatever gateway you already use for those.

## Supported versions

| Version | Supported |
|---|---|
| 2.x | yes |
| 1.x | no. Upgrade; fingerprints have to be rebuilt |
