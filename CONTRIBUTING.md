# Contributing to AudioFP

Thanks for helping. This page tells you how the project is organised, how to run it locally, and what a good pull request looks like.

## Set up a development environment

```bash
git clone https://github.com/inboxpraveen/Audio-Fingerprint.git
cd Audio-Fingerprint
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                              # runtime + pytest, ruff, psutil
pre-commit install                                   # optional: ruff on every commit
audiofp doctor                                       # verify libsndfile / ffmpeg / storage
```

Run the server in development mode (Flask reloader is off by default; restart after Python changes, static files are served fresh):

```bash
audiofp serve --profile development
```

## Run the checks

```bash
ruff check fingerprint tests run.py && ruff format --check fingerprint tests run.py
pytest -q                          # unit + API + CLI, ~10 s, synthetic audio only
pytest -q -m slow                  # 30-minute-file scale test (needs psutil)
AUDIOFP_TEST_POSTGRES_DSN=postgresql://audiofp:audiofp@localhost:5432/audiofp pytest -q tests/test_storage.py
```

CI runs the same commands on Linux, Windows and macOS, plus the PostgreSQL contract tests against a service container and a Docker image build.

## Where things live

| Area | Path | Notes |
|---|---|---|
| Settings | `fingerprint/config.py` | add a field with `metadata=_doc(...)`; docs/CONFIGURATION.md is generated from it |
| Formats | `fingerprint/formats.py` | the only place extensions are listed |
| Signal processing | `fingerprint/core/` | decoder → fingerprinter → hash_generator → matcher |
| Storage | `fingerprint/storage/` | implement `StorageBackend`; all backends must pass `tests/test_storage.py` |
| Indexing | `fingerprint/indexing/` | scanner, `Indexer`, progress |
| Jobs | `fingerprint/jobs/manager.py` | storage-agnostic background jobs |
| API | `fingerprint/api/` | `runtime.py` (services), `routes/` (thin handlers), `openapi.py` (a test enforces coverage) |
| CLI | `fingerprint/cli.py` | argparse; keep `--json` output stable |
| UI | `fingerprint/static/` | vanilla HTML/CSS/JS, no build step |

## Guidelines

- **Tests come with the change.** New behaviour needs a test; bug fixes need a regression test. Audio in tests is generated (`tests/conftest.py::synth_signal`) — never commit binary fixtures.
- **Keep errors actionable.** Raise the exception type from `fingerprint/utils/exceptions.py` with a message that tells the user what to do next; the API maps it to a status and code automatically.
- **Never break the fingerprint contract silently.** If a change alters the peaks or hashes produced for the same audio (STFT framing, peak picking, hash layout, pairing rules), bump `FINGERPRINT_ALGORITHM_VERSION` in `fingerprint/config.py` and say in `CHANGELOG.md` that re-indexing is required. New *parameters* must be added to the fingerprint signature with `fingerprint=True`.
- **Every route is documented.** Add or change endpoints in `fingerprint/api/openapi.py` too — `tests/test_api.py::test_openapi_covers_every_route` fails otherwise — and update `docs/API.md`.
- **Log, don't print.** `logger = logging.getLogger(__name__)`; per-file failures are warnings, unexpected exceptions use `logger.exception`.
- **Style** is enforced by ruff (`pyproject.toml`). Type hints on public functions, docstrings that explain *why*.
- **Keep the UI dependency-free.** No frameworks or bundlers; the whole UI is three static files.

## Pull requests

1. Open an issue first for anything larger than a bug fix, so the design can be discussed.
2. Branch from `main`, keep commits focused, write a clear PR description (the template asks for the checklist).
3. Make sure `ruff` and `pytest` pass locally.
4. Update docs (`README.md`, `docs/*.md`, `CHANGELOG.md` under "Unreleased") when behaviour changes.

## Reporting bugs

Use the bug template. Include the output of `audiofp doctor`, the request id from the API error (also in the server log line), and — if it is a matching problem — the numbers from `audiofp search --json` (aligned hashes, confidence, peak ratio) so the thresholds can be reasoned about.

## Code of conduct

Be kind and constructive; see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
