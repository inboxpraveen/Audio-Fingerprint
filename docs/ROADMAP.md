# Roadmap

AudioFP started as a Shazam-style song search. Version 2 turned it into a general **audio pattern search** service with production plumbing. This page records where it goes next and why the current design is shaped the way it is. Nothing below is implemented unless stated.

## Guiding idea

Contact-centre quality assurance needs answers like *"was the mandatory disclaimer played?"*, *"how long was the customer on hold?"*, *"did the agent use the approved greeting?"*, *"which calls contain this advertisement?"*. Some of those are **acoustic** questions (the same audio recurring) — AudioFP answers them today with `occurrences` mode. Others are **linguistic** (words and phrases spoken by different people) — that needs speech recognition. The plan is to serve both from one library, one job system and one UI, and to let evaluation rules combine them.

## Milestones

### 2.x — acoustic QA workflows (no new algorithms)
- Saved **pattern sets**: name a group of tracks (e.g. "compliance phrases EN"), run one recording against a set, get a report per pattern (found / not found / count / timestamps).
- **Batch evaluation jobs**: point at a folder of calls, produce a CSV/JSON report of occurrences per call. The `JobManager` and `Indexer.index_paths` already provide the machinery.
- **Silence / hold detection** as cheap signal-level features (RMS-based), stored per track as metadata.
- Webhooks on job completion; export/import of a library.

### 3.0 — keyword and phrase search on audio
- A second engine that produces **transcripts with word timings** (e.g. [faster-whisper](https://github.com/SYSTRAN/faster-whisper)) and stores them next to the fingerprints. Search by text, get timestamps, jump to the position in the player — the same UI affordances as fingerprint matches.
- Engines are selected per track and per search (`engine=fingerprint|transcript`); tracks, tags, jobs, auth, storage and the UI stay shared. See [EXTENDING.md](EXTENDING.md#where-a-future-keyword--transcript-engine-plugs-in).
- Evaluation rules: "phrase X must occur within the first 30 s", "pattern Y must not occur", scored per call, with a dashboard.

### Later
- Speaker turn detection to attribute phrases to agent vs. customer.
- Horizontal scaling: a queue-backed worker model (the job manager is deliberately storage-agnostic so it can be swapped for Celery/RQ).
- Fingerprint robustness to time-stretching/pitch-shifting (not needed for QA, useful for music).

## Design decisions that keep the door open

- **Tracks, not songs.** Every indexed item is a *track* with tags and free-form metadata; nothing assumes music.
- **Signed offsets and spans** in match results describe *where* things overlap in both directions, which is what timelines and rules need.
- **One storage contract** (`StorageBackend`) with contract tests, so a transcript index can live in the same SQLite/PostgreSQL database.
- **Jobs are generic**: `JobManager.submit(type, label, runner)` runs anything, persists progress and survives restarts.
- **Uniform errors, request ids, JSON logs**: operational plumbing does not have to be rebuilt per feature.

Have a use case that is not listed? Open an issue — real workflows shape the priorities.
