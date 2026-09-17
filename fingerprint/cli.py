"""The ``audiofp`` command line interface.

    audiofp serve                       # start the web UI + API
    audiofp index ./recordings          # index a folder (with progress bar)
    audiofp search clip.wav             # identify a clip
    audiofp search call.wav --mode occurrences
    audiofp tracks list --q "jingle"
    audiofp stats
    audiofp doctor                      # check dependencies, ffmpeg, storage, config
    audiofp config                      # show effective configuration
    audiofp db reset --yes              # wipe the fingerprint database

Also available as ``python -m fingerprint`` and (for compatibility) ``python run.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import signal
import sys
import time
from typing import Any

from . import __version__, formats
from .config import ENV_PREFIX, Settings, describe_settings
from .utils.exceptions import AudioFPError
from .utils.logging import configure_logging

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings_from_args(args: argparse.Namespace, **extra: Any) -> Settings:
    overrides: dict[str, Any] = {}
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = args.data_dir
    if getattr(args, "storage", None):
        overrides["storage_type"] = args.storage
    if getattr(args, "sqlite_path", None):
        overrides["sqlite_path"] = args.sqlite_path
    if getattr(args, "log_level", None):
        overrides["log_level"] = args.log_level
    overrides.update({k: v for k, v in extra.items() if v is not None})
    return Settings.load(profile=getattr(args, "profile", None), **overrides)


def _runtime(args: argparse.Namespace, **extra: Any):
    from .api.runtime import Runtime

    settings = _settings_from_args(args, **extra)
    configure_logging(settings.log_level if not getattr(args, "quiet", False) else "WARNING", settings.log_format, None)
    return Runtime(settings)


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _install_shutdown_signals() -> None:
    """Turn SIGTERM (systemd stop, docker stop) into a clean shutdown.

    Python's default SIGTERM disposition kills the process outright - no ``finally`` blocks, no ``atexit`` -
    which would drop fingerprints still buffered in the SQLite write batch. Raising ``SystemExit`` instead
    unwinds the server loop so :meth:`Runtime.close` can cancel jobs and flush.
    """

    def _terminate(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError, AttributeError):  # pragma: no cover - not the main thread / no SIGTERM
        pass


def cmd_serve(args: argparse.Namespace) -> int:
    from .api.app import create_app

    settings = _settings_from_args(args, host=args.host, port=args.port)
    app = create_app(settings=settings)
    _install_shutdown_signals()
    try:
        return _serve(app, settings, args)
    finally:
        app.extensions["audiofp"].close()


def _serve(app: Any, settings: Settings, args: argparse.Namespace) -> int:
    host, port = settings.host, settings.port
    server = args.server
    if server == "auto":
        server = "flask" if settings.profile == "development" else "waitress"

    url_host = "localhost" if host in ("0.0.0.0", "::", "127.0.0.1") else host
    print(f"\n  AudioFP {__version__} ({settings.profile}, storage={settings.storage_type})")
    print(f"  UI:   http://{url_host}:{port}/")
    print(f"  API:  http://{url_host}:{port}/api/v1   (docs: /docs)")
    if host == "0.0.0.0":
        print(
            "  Note: listening on all interfaces" + ("" if settings.api_key else " with NO API key - set AUDIOFP_API_KEY if this port is reachable by others")
        )
    print()

    if server == "flask":
        app.run(host=host, port=port, debug=settings.debug and not settings.is_production, threaded=True, use_reloader=False)
        return EXIT_OK
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print("waitress is not installed (pip install waitress); falling back to Flask's development server.", file=sys.stderr)
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
        return EXIT_OK
    waitress_serve(
        app,
        host=host,
        port=port,
        threads=args.threads or settings.server_threads,
        channel_timeout=600,
        max_request_body_size=settings.max_content_length,
        ident="AudioFP",
    )
    return EXIT_OK


def cmd_index(args: argparse.Namespace) -> int:
    from .indexing import find_media_files, print_progress
    from .indexing.progress import ProgressTracker

    rt = _runtime(args, index_workers=args.workers)
    try:
        paths: list[str] = []
        for target in args.paths:
            target = os.path.abspath(os.path.expanduser(target))
            if os.path.isdir(target):
                paths.extend(find_media_files(target, recursive=not args.no_recursive))
            elif os.path.isfile(target):
                paths.append(target)
            else:
                print(f"error: {target} does not exist", file=sys.stderr)
                return EXIT_USAGE
        if not paths:
            print("No supported media files found.", file=sys.stderr)
            return EXIT_ERROR
        print(f"Indexing {len(paths)} file(s) with {rt.settings.effective_index_workers} worker(s) into {rt.storage.backend_name} storage ...", file=sys.stderr)
        tracker = ProgressTracker(total=len(paths))

        def on_progress(_path, _outcome, trk):
            if not args.quiet:
                print_progress(trk.snapshot())

        summary = rt.indexer.index_paths(paths, tags=args.tags, source="cli", progress=on_progress, tracker=tracker)
        if args.json:
            _print_json(summary.to_dict())
        else:
            print(f"\nDone in {summary.elapsed_sec:.1f}s: {summary.indexed} indexed, {summary.duplicates} duplicate(s) skipped, {summary.failed} failed.")
            for err in summary.errors[:20]:
                print(f"  ! {os.path.basename(err['file'])}: {err['error']}")
            if len(summary.errors) > 20:
                print(f"  ... and {len(summary.errors) - 20} more (use --json for the full list)")
        return EXIT_OK if summary.failed == 0 else EXIT_ERROR
    finally:
        rt.close()


def cmd_search(args: argparse.Namespace) -> int:
    from .api.responses import format_search

    rt = _runtime(args)
    try:
        path = os.path.abspath(os.path.expanduser(args.clip))
        if not os.path.isfile(path):
            print(f"error: {path} does not exist", file=sys.stderr)
            return EXIT_USAGE
        result = rt.search_file(
            path,
            filename=os.path.basename(path),
            mode=args.mode,
            top_k=args.top_k,
            min_confidence=args.min_confidence,
            min_aligned_hashes=args.min_aligned,
            min_peak_ratio=args.min_peak_ratio,
        )
        payload = format_search(result)
        if args.json:
            _print_json(payload)
            return EXIT_OK if payload["found"] else EXIT_ERROR
        q = payload["query"]
        print(f"Query: {q['filename']}  {q['duration_sec']}s, {q['num_peaks']} peaks, {q['num_hashes']} hashes  ({payload['processing_time_ms']} ms)")
        if not payload["found"]:
            print("No match above the thresholds. Try a longer clip, or lower --min-confidence / --min-peak-ratio.")
            return EXIT_ERROR
        for i, m in enumerate(payload["matches"], 1):
            name = m.get("display_name") or m["filename"]
            print(f"\n{i}. {name}  [{m['quality']}]  confidence={m['confidence']:.3f}  aligned={m['aligned_hashes']}  peak_ratio={m['peak_ratio']}")
            if m.get("artist"):
                print(f"   artist: {m['artist']}")
            for o in m["occurrences"]:
                if o["offset_sec"] >= 0:
                    print(
                        f"   - query {_fmt_time(o['query_start_sec'])}-{_fmt_time(o['query_end_sec'])} matches track at {_fmt_time(o['track_start_sec'])}-{_fmt_time(o['track_end_sec'])} (aligned {o['aligned_hashes']}, {o['quality']})"
                    )
                else:
                    print(
                        f"   - track content found in query at {_fmt_time(o['query_start_sec'])}-{_fmt_time(o['query_end_sec'])} (track {_fmt_time(o['track_start_sec'])}-{_fmt_time(o['track_end_sec'])}; aligned {o['aligned_hashes']}, {o['quality']})"
                    )
        return EXIT_OK
    finally:
        rt.close()


def cmd_tracks(args: argparse.Namespace) -> int:
    from .api.responses import format_track

    rt = _runtime(args)
    try:
        if args.tracks_cmd == "list":
            items, total = rt.storage.list_tracks(query=args.q, sort=args.sort, order=args.order, offset=args.offset, limit=args.limit)
            if args.json:
                _print_json({"items": [format_track(t, include_path=True) for t in items], "total": total})
                return EXIT_OK
            print(f"{total} track(s)" + (f" matching '{args.q}'" if args.q else "") + f", showing {len(items)}:")
            for t in items:
                print(
                    f"  {t.track_id}  {_fmt_time(t.duration):>8}  {t.num_hashes:>8} hashes  {t.title or t.filename}"
                    + (f"  [{', '.join(t.tags)}]" if t.tags else "")
                )
            return EXIT_OK
        if args.tracks_cmd == "show":
            record = rt.storage.require_track(args.track_id)
            _print_json(format_track(record, include_path=True))
            return EXIT_OK
        if args.tracks_cmd == "delete":
            if not args.yes:
                answer = input(f"Delete {len(args.track_ids)} track(s)? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Aborted.")
                    return EXIT_ERROR
            deleted = rt.storage.delete_tracks(args.track_ids)
            print(f"Deleted {deleted} track(s).")
            return EXIT_OK
        return EXIT_USAGE
    finally:
        rt.close()


def cmd_stats(args: argparse.Namespace) -> int:
    rt = _runtime(args)
    try:
        data = rt.storage.get_stats()
        if args.full and hasattr(rt.storage, "unique_hash_count"):
            data["unique_hashes"] = rt.storage.unique_hash_count()
        if args.json:
            _print_json(data)
        else:
            print(f"Storage:        {data['storage_type']}" + (f" ({data['db_path']})" if data.get("db_path") else ""))
            print(f"Tracks:         {data['total_tracks']}")
            print(f"Hashes:         {data['total_hashes']:,}")
            print(f"Audio indexed:  {_fmt_time(data['total_duration_sec'])} (h:mm:ss)")
            if data.get("db_size_bytes"):
                print(f"Database size:  {data['db_size_bytes'] / 1e6:.1f} MB")
            if "unique_hashes" in data:
                print(f"Unique hashes:  {data['unique_hashes']:,}")
        return EXIT_OK
    finally:
        rt.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the environment and report what works, what is degraded, what is broken."""
    from .core.decoder import ffmpeg_info

    problems = 0
    warnings = 0

    def ok(msg: str) -> None:
        print(f"  [ok]   {msg}")

    def warn(msg: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"  [warn] {msg}")

    def fail(msg: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  [FAIL] {msg}")

    print(f"AudioFP {__version__} doctor\n")
    print("Runtime")
    py = sys.version_info
    (ok if py >= (3, 10) else fail)(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}")

    print("\nDependencies")
    for module, label in (
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("soundfile", "soundfile (libsndfile)"),
        ("soxr", "soxr"),
        ("flask", "flask"),
        ("flask_cors", "flask-cors"),
    ):
        try:
            mod = __import__(module)
            version = getattr(mod, "__version__", "")
            extra = ""
            if module == "soundfile":
                extra = f", libsndfile {mod.__libsndfile_version__}"
                fmts = mod.available_formats()
                missing = [f for f in ("MP3", "FLAC", "OGG") if f not in fmts]
                if missing:
                    warn(f"libsndfile lacks {', '.join(missing)} support; those files will need ffmpeg")
            ok(f"{label} {version}{extra}")
        except Exception as exc:
            fail(f"{label} missing or broken: {exc}")
    try:
        import waitress  # noqa: F401

        ok("waitress (production server)")
    except ImportError:
        warn("waitress not installed - `audiofp serve --profile production` will fall back to Flask's dev server")

    print("\nffmpeg")
    ff = ffmpeg_info()
    if ff.available:
        ok(f"ffmpeg {ff.version or ''} at {ff.path}")
    else:
        warn(
            "ffmpeg not found: video files and M4A/AAC/WMA cannot be decoded. Install it: winget install Gyan.FFmpeg | brew install ffmpeg | apt install ffmpeg"
        )
    print(f"       native audio formats: {', '.join(formats.describe_formats()['native_audio'])}")

    print("\nConfiguration")
    try:
        settings = _settings_from_args(args)
        ok(f"profile={settings.profile} storage={settings.storage_type} data_dir={os.path.abspath(settings.data_dir)}")
        ok(f"fingerprint signature {settings.fingerprint_signature()} ({', '.join(f'{k}={v}' for k, v in settings.fingerprint_params().items())})")
        if settings.is_production and not settings.api_key:
            warn("production profile without AUDIOFP_API_KEY - anyone who can reach the port can use the API")
        if settings.is_production and settings.allow_directory_indexing and not settings.index_roots:
            warn("directory indexing is effectively disabled in production until AUDIOFP_INDEX_ROOTS is set")
        for label, path in (("data dir", settings.data_dir), ("upload dir", settings.upload_dir_resolved)):
            try:
                os.makedirs(path, exist_ok=True)
                probe = os.path.join(path, ".write-test")
                with open(probe, "w") as fh:
                    fh.write("ok")
                os.remove(probe)
                ok(f"{label} writable: {os.path.abspath(path)}")
            except OSError as exc:
                fail(f"{label} not writable ({path}): {exc}")
        from .utils.files import free_space_bytes, human_size

        free = free_space_bytes(settings.data_dir)
        if free is not None:
            (ok if free > 1024**3 else warn)(f"free disk space: {human_size(free)}")
    except AudioFPError as exc:
        fail(f"configuration error: {exc.message}")
        settings = None

    print("\nStorage")
    if settings is not None:
        try:
            from .storage import create_storage

            store = create_storage(settings)
            stats = store.get_stats()
            ok(f"{stats['storage_type']} reachable: {stats['total_tracks']} tracks, {stats['total_hashes']:,} hashes")
            store.close()
        except AudioFPError as exc:
            fail(f"{exc.code}: {exc.message}")
        except Exception as exc:
            fail(f"storage error: {exc}")

    print()
    if problems:
        print(f"{problems} problem(s), {warnings} warning(s). Fix the [FAIL] items above.")
        return EXIT_ERROR
    print(f"All checks passed{f' with {warnings} warning(s)' if warnings else ''}.")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    if args.describe:
        rows = describe_settings()
        if args.json:
            _print_json(rows)
            return EXIT_OK
        print("| Setting | Environment variable | Type | Default | Description |")
        print("|---|---|---|---|---|")
        for r in rows:
            default = r["default"]
            default_str = "" if default in ("", [], None) else f"`{default}`"
            flag = " *(fingerprint)*" if r["fingerprint"] else ""
            print(f"| `{r['name']}` | `{r['env']}` | {r['type']} | {default_str} | {r['help']}{flag} |")
        return EXIT_OK
    settings = _settings_from_args(args)
    data = settings.public_dict()
    data["_fingerprint_signature"] = settings.fingerprint_signature()
    data["_env_prefix"] = ENV_PREFIX
    if args.json:
        _print_json(data)
    else:
        width = max(len(k) for k in data)
        for key, value in data.items():
            print(f"{key.ljust(width)}  {value}")
    return EXIT_OK


def cmd_db(args: argparse.Namespace) -> int:
    from .storage import create_storage

    settings = _settings_from_args(args)
    if args.db_cmd == "reset":
        if not args.yes:
            answer = input(f"This deletes EVERY track and fingerprint in the {settings.storage_type} store. Continue? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return EXIT_ERROR
        store = create_storage(settings, check_compat=False)
        try:
            store.clear()
            store.set_meta("fingerprint_signature", settings.fingerprint_signature())
            store.set_meta("fingerprint_params", json.dumps(settings.fingerprint_params(), sort_keys=True))
            if hasattr(store, "vacuum"):
                store.vacuum()
        finally:
            store.close()
        print("Database reset. It now uses the current fingerprint parameters.")
        return EXIT_OK
    if args.db_cmd == "check":
        try:
            store = create_storage(settings)
        except AudioFPError as exc:
            print(f"[FAIL] {exc.code}: {exc.message}")
            return EXIT_ERROR
        try:
            stats = store.get_stats()
            print(
                f"[ok] {stats['storage_type']}: {stats['total_tracks']} tracks, {stats['total_hashes']:,} hashes, signature {store.get_meta('fingerprint_signature')}"
            )
        finally:
            store.close()
        return EXIT_OK
    if args.db_cmd == "vacuum":
        store = create_storage(settings)
        try:
            if hasattr(store, "checkpoint"):
                store.checkpoint()
            if hasattr(store, "vacuum"):
                store.vacuum()
                print("Vacuum complete.")
            else:
                print("Vacuum is not applicable to this backend.")
        finally:
            store.close()
        return EXIT_OK
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audiofp", description="AudioFP - self-hosted audio fingerprinting and audio pattern search.")
    parser.add_argument("--version", action="version", version=f"audiofp {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", choices=("development", "production", "testing"), help="Configuration profile (default: $AUDIOFP_PROFILE or development)")
    common.add_argument("--data-dir", help="Data directory (default: ./data)")
    common.add_argument("--storage", choices=("memory", "sqlite", "postgres"), help="Storage backend override")
    common.add_argument("--sqlite-path", help="SQLite database file override")
    common.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="Log level override")
    common.add_argument("--quiet", "-q", action="store_true", help="Less output")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("serve", parents=[common], help="Start the web UI and REST API")
    p.add_argument("--host", help="Bind address (default from config; 127.0.0.1 in development)")
    p.add_argument("--port", type=int, help="Port (default 5000)")
    p.add_argument("--server", choices=("auto", "flask", "waitress"), default="auto", help="auto = flask in development, waitress otherwise")
    p.add_argument("--threads", type=int, help="Waitress worker threads")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("index", parents=[common], help="Index files or folders")
    p.add_argument("paths", nargs="+", help="Files and/or directories")
    p.add_argument("--no-recursive", action="store_true", help="Do not descend into sub-folders")
    p.add_argument("--workers", type=int, help="Parallel fingerprint workers")
    p.add_argument("--tags", help="Comma-separated tags to attach to every indexed track")
    p.add_argument("--json", action="store_true", help="Print a JSON summary")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", parents=[common], help="Search the library with a clip")
    p.add_argument("clip")
    p.add_argument("--mode", choices=("identify", "occurrences"), default=None)
    p.add_argument("--top-k", type=int)
    p.add_argument("--min-confidence", type=float)
    p.add_argument("--min-aligned", type=int)
    p.add_argument("--min-peak-ratio", type=float)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("tracks", parents=[common], help="List, show or delete tracks")
    parser.set_defaults(_subparsers={})
    parser.get_default("_subparsers")["tracks"] = p
    tsub = p.add_subparsers(dest="tracks_cmd", metavar="<subcommand>")
    tl = tsub.add_parser("list", parents=[common], help="List tracks")
    tl.add_argument("--q", help="Search text")
    tl.add_argument("--sort", default="indexed_at", choices=("indexed_at", "title", "artist", "duration", "filename", "num_hashes"))
    tl.add_argument("--order", default="desc", choices=("asc", "desc"))
    tl.add_argument("--limit", type=int, default=50)
    tl.add_argument("--offset", type=int, default=0)
    tl.add_argument("--json", action="store_true")
    ts = tsub.add_parser("show", parents=[common], help="Show one track as JSON")
    ts.add_argument("track_id")
    td = tsub.add_parser("delete", parents=[common], help="Delete tracks")
    td.add_argument("track_ids", nargs="+")
    td.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_tracks)

    p = sub.add_parser("stats", parents=[common], help="Library statistics")
    p.add_argument("--full", action="store_true", help="Also count unique hashes (slow on large databases)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", parents=[common], help="Check the environment")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("config", parents=[common], help="Show effective configuration")
    p.add_argument("--describe", action="store_true", help="Print the option reference table (Markdown)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("db", parents=[common], help="Database maintenance")
    parser.get_default("_subparsers")["db"] = p
    dsub = p.add_subparsers(dest="db_cmd", metavar="<subcommand>")
    dr = dsub.add_parser("reset", parents=[common], help="Delete all tracks and fingerprints")
    dr.add_argument("--yes", "-y", action="store_true")
    dsub.add_parser("check", parents=[common], help="Verify the database opens and is compatible")
    dsub.add_parser("vacuum", parents=[common], help="Compact the database (SQLite)")
    p.set_defaults(func=cmd_db)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE
    if args.command == "tracks" and not getattr(args, "tracks_cmd", None):
        args._subparsers["tracks"].print_help()
        return EXIT_USAGE
    if args.command == "db" and not getattr(args, "db_cmd", None):
        args._subparsers["db"].print_help()
        return EXIT_USAGE
    started = time.time()
    try:
        return int(args.func(args))
    except AudioFPError as exc:
        print(f"error ({exc.code}): {exc.message}", file=sys.stderr)
        if exc.details and not getattr(args, "quiet", False):
            print(f"details: {json.dumps(exc.details, default=str)}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"\nInterrupted after {time.time() - started:.1f}s", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
