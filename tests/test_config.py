import pytest

from fingerprint.config import ENV_PREFIX, Settings, describe_settings
from fingerprint.utils.exceptions import ConfigurationError


def test_defaults_and_profiles():
    dev = Settings.load(dotenv=False, env={})
    assert dev.profile == "development" and dev.debug is True and dev.cors_origins == []  # same-origin only, even in dev
    prod = Settings.load(dotenv=False, env={}, profile="production")
    assert prod.debug is False and prod.host == "0.0.0.0" and prod.is_production
    test = Settings.load(dotenv=False, env={}, profile="testing")
    assert test.storage_type == "memory" and test.persist_jobs is False


def test_env_overrides_and_coercion():
    env = {
        f"{ENV_PREFIX}PROFILE": "production",
        f"{ENV_PREFIX}PORT": "8080",
        f"{ENV_PREFIX}DEBUG": "true",
        f"{ENV_PREFIX}MIN_CONFIDENCE": "0.25",
        f"{ENV_PREFIX}INDEX_ROOTS": "/srv/a, /srv/b",
        f"{ENV_PREFIX}CORS_ORIGINS": "",
    }
    s = Settings.load(dotenv=False, env=env)
    assert s.profile == "production" and s.port == 8080 and s.debug is True
    assert s.min_confidence == 0.25 and s.index_roots == ["/srv/a", "/srv/b"]
    assert s.cors_origins == []  # empty env value keeps the default (none)


def test_explicit_overrides_win():
    s = Settings.load(dotenv=False, env={f"{ENV_PREFIX}PORT": "1"}, port=9)
    assert s.port == 9
    with pytest.raises(ConfigurationError):
        Settings.load(dotenv=False, env={}, no_such_setting=1)


@pytest.mark.parametrize(
    "env,fragment",
    [
        ({f"{ENV_PREFIX}PORT": "abc"}, "PORT"),
        ({f"{ENV_PREFIX}DEBUG": "maybe"}, "DEBUG"),
        ({f"{ENV_PREFIX}STORAGE_TYPE": "redis"}, "storage_type"),
        ({f"{ENV_PREFIX}STORAGE_TYPE": "postgres"}, "postgres_dsn"),
        ({f"{ENV_PREFIX}N_FFT": "2047"}, "n_fft"),
        ({f"{ENV_PREFIX}HOP_LENGTH": "4096"}, "hop_length"),
        ({f"{ENV_PREFIX}MAX_HASH_TIME_DELTA": "9999"}, "max_hash_time_delta"),
        ({f"{ENV_PREFIX}LOG_FORMAT": "xml"}, "log_format"),
        ({f"{ENV_PREFIX}FINGERPRINT_COMPAT": "loose"}, "fingerprint_compat"),
    ],
)
def test_validation_errors(env, fragment):
    with pytest.raises(ConfigurationError) as exc:
        Settings.load(dotenv=False, env=env)
    assert fragment in str(exc.value)


def test_dotenv_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        f"{ENV_PREFIX}PORT=7777\n"
        f"export {ENV_PREFIX}DATA_DIR='my data'\n"
        f'{ENV_PREFIX}LOG_LEVEL="WARNING"  \n'
        f"{ENV_PREFIX}TOP_K=3 # trailing comment\n"
        "UNRELATED=1\n"
    )
    monkeypatch.chdir(tmp_path)
    s = Settings.load(env={})
    assert s.port == 7777 and s.data_dir == "my data" and s.log_level == "WARNING" and s.top_k == 3
    # real environment beats .env
    s2 = Settings.load(env={f"{ENV_PREFIX}PORT": "1"})
    assert s2.port == 1


def test_fingerprint_signature_tracks_only_algorithm_params():
    a = Settings.load(dotenv=False, env={})
    b = Settings.load(dotenv=False, env={}, port=1234, top_k=9, log_level="ERROR")
    c = Settings.load(dotenv=False, env={}, fan_value=11)
    assert a.fingerprint_signature() == b.fingerprint_signature()
    assert a.fingerprint_signature() != c.fingerprint_signature()
    assert "fan_value" in a.fingerprint_params() and "algorithm_version" in a.fingerprint_params()


def test_public_dict_redacts_secrets():
    s = Settings.load(dotenv=False, env={}, api_key="hunter2", postgres_dsn="")
    public = s.public_dict()
    assert public["api_key"] == "***" and public["postgres_dsn"] == ""


def test_describe_settings_has_docs_for_every_field():
    rows = describe_settings()
    assert rows and all(r["help"] for r in rows)
    names = {r["name"] for r in rows}
    assert {"sample_rate", "api_key", "index_roots", "min_peak_ratio"} <= names


def test_log_file_resolution(tmp_path):
    prod = Settings.load(dotenv=False, env={}, profile="production", data_dir=str(tmp_path))
    assert prod.log_file == "auto" and prod.log_file_resolved == str(tmp_path / "logs" / "audiofp.log")
    dev = Settings.load(dotenv=False, env={}, data_dir=str(tmp_path))
    assert dev.log_file_resolved == ""
    off = Settings.load(dotenv=False, env={f"{ENV_PREFIX}LOG_FILE": ""}, profile="production", data_dir=str(tmp_path))
    assert off.log_file_resolved == ""  # an empty AUDIOFP_LOG_FILE disables the file (Docker image default)
    none = Settings.load(dotenv=False, env={f"{ENV_PREFIX}LOG_FILE": "none"}, profile="production")
    assert none.log_file_resolved == ""
    explicit = Settings.load(dotenv=False, env={f"{ENV_PREFIX}LOG_FILE": "/var/log/audiofp.log"})
    assert explicit.log_file_resolved == "/var/log/audiofp.log"
    # an empty value for a numeric setting is ignored and the default stays
    assert Settings.load(dotenv=False, env={f"{ENV_PREFIX}PORT": ""}).port == 5000


def test_derived_paths(tmp_path):
    s = Settings.load(dotenv=False, env={}, data_dir=str(tmp_path))
    assert s.sqlite_path_resolved.endswith("fingerprints.db")
    assert s.upload_dir_resolved.endswith("uploads")
    assert s.max_content_length == s.max_upload_mb * 1024 * 1024
    assert s.effective_index_workers >= 1
