"""The ``audiofp`` command line interface."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from fingerprint.cli import EXIT_ERROR, EXIT_OK, EXIT_USAGE, _install_shutdown_signals, main


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated data dir + SQLite database for CLI runs."""
    monkeypatch.chdir(tmp_path)
    for var in list(__import__("os").environ):
        if var.startswith("AUDIOFP_"):
            monkeypatch.delenv(var)
    return ["--profile", "testing", "--storage", "sqlite", "--sqlite-path", str(tmp_path / "cli.db"), "--data-dir", str(tmp_path / "data"), "--quiet"]


def test_help_and_usage(capsys):
    assert main([]) == EXIT_USAGE
    assert main(["tracks"]) == EXIT_USAGE
    assert main(["db"]) == EXIT_USAGE
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "audiofp" in capsys.readouterr().out


def test_config_and_doctor(cli_env, capsys):
    assert main(["config", "--json", *cli_env]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["storage_type"] == "sqlite" and data["_fingerprint_signature"] and data["api_key"] == ""
    assert main(["config", "--describe"]) == EXIT_OK
    table = capsys.readouterr().out
    assert "AUDIOFP_SAMPLE_RATE" in table and "| Setting |" in table
    assert main(["doctor", *cli_env]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[ok]" in out and "Storage" in out and "fingerprint signature" in out


def test_index_search_stats_tracks_db(cli_env, audio_dir, capsys):
    assert main(["index", str(audio_dir), *cli_env]) == EXIT_ERROR  # broken.wav fails -> non-zero exit, others indexed
    out = capsys.readouterr().out
    assert "6 indexed" in out and "1 failed" in out

    assert main(["index", str(audio_dir), "--json", *cli_env]) == EXIT_ERROR
    summary = json.loads(capsys.readouterr().out)
    assert summary["duplicates"] == 6 and summary["indexed"] == 0

    assert main(["stats", "--full", *cli_env]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Tracks:         6" in out and "Unique hashes" in out

    # The clip itself was indexed too (it is in the folder), so it matches itself first and the song second.
    assert main(["search", str(audio_dir / "clip_second_song_at_12s.wav"), *cli_env]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Second Song" in out and "matches track at 0:12" in out

    assert main(["search", str(audio_dir / "clip_second_song_at_12s.wav"), "--json", "--top-k", "3", *cli_env]) == EXIT_OK
    result = json.loads(capsys.readouterr().out)
    titles = [m["title"] for m in result["matches"]]
    assert result["found"] and titles[0] == "clip_second_song_at_12s" and "Second Song" in titles  # noisy copy ranks in between

    from .conftest import synth_signal, write_wav

    unrelated = write_wav(audio_dir.parent / "unrelated_not_indexed.wav", synth_signal(seed=4321, seconds=4))
    assert main(["search", unrelated, *cli_env]) == EXIT_ERROR
    assert "No match" in capsys.readouterr().out
    assert main(["search", str(audio_dir / "missing.wav"), *cli_env]) == EXIT_USAGE

    assert main(["tracks", "list", "--q", "call", "--json", *cli_env]) == EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    assert listing["total"] == 1
    track_id = listing["items"][0]["track_id"]
    assert main(["tracks", "show", track_id, *cli_env]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["track_id"] == track_id
    assert main(["tracks", "delete", track_id, "--yes", *cli_env]) == EXIT_OK
    assert main(["tracks", "list", *cli_env]) == EXIT_OK
    assert "5 track(s)" in capsys.readouterr().out
    assert main(["tracks", "show", "nope", *cli_env]) == EXIT_ERROR
    assert "not_found" in capsys.readouterr().err

    assert main(["db", "check", *cli_env]) == EXIT_OK
    assert main(["db", "vacuum", *cli_env]) == EXIT_OK
    assert main(["db", "reset", "--yes", *cli_env]) == EXIT_OK
    capsys.readouterr()
    assert main(["stats", "--json", *cli_env]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["total_tracks"] == 0


def test_index_nonexistent_path(cli_env, capsys, tmp_path):
    assert main(["index", str(tmp_path / "nowhere"), *cli_env]) == EXIT_USAGE
    (tmp_path / "empty").mkdir()
    assert main(["index", str(tmp_path / "empty"), *cli_env]) == EXIT_ERROR


def test_run_py_translates_legacy_flags():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("run_compat", Path(__file__).resolve().parents[1] / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert module._translate(["--env", "production", "--port", "8080"]) == ["serve", "--profile", "production", "--port", "8080"]
    assert module._translate(["--env=development"]) == ["serve", "--profile", "development"]


def test_sigterm_becomes_clean_shutdown():
    """`audiofp serve` turns SIGTERM into SystemExit so the storage buffer is flushed on `systemctl stop` / `docker stop`."""
    previous = signal.getsignal(signal.SIGTERM)
    try:
        _install_shutdown_signals()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not previous
        with pytest.raises(SystemExit) as exc:
            handler(signal.SIGTERM, None)
        assert exc.value.code == 128 + int(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM cannot be delivered to a child process on Windows")
def test_serve_stops_cleanly_on_sigterm(tmp_path):
    """End to end: `audiofp serve` under waitress exits promptly and without a traceback on SIGTERM."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "AUDIOFP_PROFILE": "testing", "AUDIOFP_STORAGE_TYPE": "sqlite", "AUDIOFP_LOG_LEVEL": "INFO"}
    cmd = [sys.executable, "-m", "fingerprint", "serve", "--server", "waitress", "--port", str(port), "--data-dir", str(tmp_path)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=1) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError("server exited early: " + proc.stdout.read()) from None
                time.sleep(0.1)
        else:
            raise AssertionError("server did not come up")
        proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=20)[0]
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode in (0, 128 + int(signal.SIGTERM)), output
    assert "Traceback" not in output, output
