# Contributing to AudioFP

Thanks for taking the time. This page covers how the project is laid out, how to run it locally and what we look for in a pull request.

## Set up a development environment

```bash
git clone https://github.com/inboxpraveen/Audio-Fingerprint.git
cd Audio-Fingerprint
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                              # runtime + pytest, ruff, psutil
pre-commit install                                   # optional: ruff on every commit
audiofp doctor                                       # verify libsndfile / ffmpeg / storage
```

Run the server in development mode. The Flask reloader is off by default, so restart after Python changes. Static files are read from disk on every request, so UI changes show up on refresh.

```bash
audiofp serve --profile development
```

## Run the checks

```bash
ruff check fingerprint tests run.py && ruff format --check fingerprint tests run.py
pytest -q                          # unit + API + CLI, about 10 s, synthetic audio only
pytest -q -m slow                  # 30-minute-file scale test (needs psutil)
AUDIOFP_TEST_POSTGRES_DSN=postgresql://audiofp:audiofp@localhost:5432/audiofp pytest -q tests/test_storage.py
```

CI runs the same commands on Linux, Windows and macOS. It also runs the PostgreSQL contract tests against a service container and builds the Docker image.

## Where things live

| Area | Path | Notes |
|---|---|---|
| Settings | `fingerprint/config.py` | add a field with `metadata=_doc(...)`; the tables in docs/CONFIGURATION.md are generated from it |
| Formats | `fingerprint/formats.py` | the only place extensions are listed |
| Signal processing | `fingerprint/core/` | decoder, then fingerprinter, hash_generator and matcher |
| Storage | `fingerprint/storage/` | implement `StorageBackend`; every backend has to pass `tests/test_storage.py` |
| Indexing | `fingerprint/indexing/` | scanner, `Indexer`, progress |
| Jobs | `fingerprint/jobs/manager.py` | background jobs, independent of the storage backend |
| API | `fingerprint/api/` | `runtime.py` (services), `routes/` (thin handlers), `openapi.py` (a test checks that every route is in it) |
| CLI | `fingerprint/cli.py` | argparse; keep the `--json` output stable |
| UI | `fingerprint/static/` | plain HTML, CSS and JS, no build step |

## Guidelines

- Changes come with tests. New behaviour needs a test, and a bug fix needs one that would have caught the bug. Test audio is generated in `tests/conftest.py::synth_signal`; please don't commit binary fixtures.
- Keep errors useful. Raise the right type from `fingerprint/utils/exceptions.py` with a message that says what to do next. The API turns it into a status and a code on its own.
- Don't change the fingerprint contract quietly. If a change alters the peaks or hashes produced for the same audio (STFT framing, peak picking, hash layout, pairing rules), bump `FINGERPRINT_ALGORITHM_VERSION` in `fingerprint/config.py` and say in `CHANGELOG.md` that re-indexing is required. New parameters that affect fingerprints go into the signature with `fingerprint=True`.
- Every route is documented. When you add or change an endpoint, update `fingerprint/api/openapi.py` (otherwise `tests/test_api.py::test_openapi_covers_every_route` fails) and `docs/API.md`.
- Log, don't print. Use `logger = logging.getLogger(__name__)`. Per-file failures are warnings; unexpected exceptions go through `logger.exception`.
- Style is enforced by ruff (see `pyproject.toml`). Public functions get type hints, and docstrings should explain why, not what.
- The UI stays dependency-free. No frameworks, no bundlers. The whole thing is three static files.

## Pull requests

1. For anything bigger than a bug fix, open an issue first so we can talk the design through.
2. Branch from `main`, keep commits focused and write a clear PR description. The template has the checklist.
3. Make sure `ruff` and `pytest` pass locally.
4. Update the docs (`README.md`, `docs/*.md`, `CHANGELOG.md` under "Unreleased") when behaviour changes.

## Reporting bugs

Use the bug template. Include the output of `audiofp doctor` and the request id from the API error (it is also in the server log line). If it is a matching problem, add the numbers from `audiofp search --json` (aligned hashes, confidence, peak ratio) so we can reason about the thresholds.

## Code of conduct

Be kind and constructive. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
