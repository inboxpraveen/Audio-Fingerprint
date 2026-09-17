"""AudioFP: self-hosted audio fingerprinting and audio pattern search.

The package is organised as a pipeline::

    decoder  ->  fingerprinter  ->  hash_generator  ->  storage  <-  matcher

* :mod:`fingerprint.core`: signal processing (decode, peaks, hashes, matching)
* :mod:`fingerprint.storage`: pluggable persistence (memory, SQLite, PostgreSQL)
* :mod:`fingerprint.indexing`: scanning folders and indexing files
* :mod:`fingerprint.jobs`: background job manager
* :mod:`fingerprint.api`: Flask REST API and the bundled web UI
* :mod:`fingerprint.cli`: the ``audiofp`` command line interface
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
