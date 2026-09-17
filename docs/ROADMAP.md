# Roadmap

AudioFP started out as a Shazam-style song search. Version 2 turned it into a general audio pattern search service with the plumbing you need to run it for real. This page is about where it goes from here and why the current design looks the way it does. Nothing below exists yet unless it says so.

## The idea behind it

Quality assurance in a contact centre comes down to questions like: was the mandatory disclaimer played, how long was the customer on hold, did the agent use the approved greeting, which calls contain this advert. Some of these are acoustic questions, the same audio recurring in many recordings, and AudioFP answers those today with `occurrences` mode. Others are about words and phrases spoken by different people, and for those you need speech recognition. The plan is to serve both kinds from one library, one job system and one UI, and to let evaluation rules combine them.

## Milestones

### 2.x: acoustic QA workflows, no new algorithms

- Saved pattern sets. Name a group of tracks ("compliance phrases EN", say), run one recording against the set and get a report per pattern: found or not, how often, at which timestamps.
- Batch evaluation jobs. Point at a folder of calls and get a CSV or JSON report of occurrences per call. `JobManager` and `Indexer.index_paths` already provide most of the machinery.
- Silence and hold detection as cheap signal-level features (RMS based), stored per track as metadata.
- Webhooks when a job finishes. Export and import of a library.

### 3.0: keyword and phrase search on audio

- A second engine that produces transcripts with word timings (probably [faster-whisper](https://github.com/SYSTRAN/faster-whisper)) and stores them next to the fingerprints. Search by text, get timestamps, jump to the position in the player, the same way fingerprint matches work in the UI today.
- The engine is chosen per track and per search (`engine=fingerprint|transcript`). Tracks, tags, jobs, auth, storage and the UI stay shared. See [EXTENDING.md](EXTENDING.md#where-a-keyword--transcript-search-would-plug-in).
- Evaluation rules such as "phrase X must occur within the first 30 s" or "pattern Y must not occur", scored per call, with a dashboard.

### Later

- Speaker turn detection, so phrases can be attributed to the agent or the customer.
- Horizontal scaling with a queue-backed worker model. The job manager doesn't depend on the storage backend, so it can be swapped for Celery or RQ.
- Fingerprints that survive time-stretching and pitch-shifting. Not needed for QA, useful for music.

## Design decisions that keep this possible

- Everything indexed is a track with tags and free-form metadata. Nothing in the code assumes music.
- Match results carry signed offsets and spans, so they say where things overlap in both directions, which is what timelines and rules need.
- There is one storage contract, `StorageBackend`, with contract tests, so a transcript index can live in the same SQLite or PostgreSQL database.
- Jobs are generic. `JobManager.submit(type, label, runner)` runs anything, persists progress and survives restarts.
- Errors, request ids and JSON logs are uniform, so none of that has to be rebuilt for a new feature.

If your use case isn't listed, open an issue. Real workflows are what shape the priorities.
