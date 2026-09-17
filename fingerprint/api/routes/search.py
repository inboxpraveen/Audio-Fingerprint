"""POST /api/v1/search: identify a clip or find every occurrence of a pattern."""

from __future__ import annotations

import os
import tempfile

from flask import jsonify, request

from ...core import MODES
from ...formats import extension_of
from ..responses import format_search
from ..validators import parse_choice, parse_float, parse_int, require_upload
from . import api_bp, runtime


@api_bp.route("/search", methods=["POST"])
def search():
    """Search the library with an uploaded clip.

    Form fields (multipart/form-data):
        audio               the clip (any supported audio/video format)
        mode                identify (default) | occurrences
        top_k               max tracks to return
        min_confidence      0 to 1
        min_aligned_hashes  integer
        min_peak_ratio      float
        max_occurrences     per track, occurrences mode only
    """
    rt = runtime()
    upload = require_upload(request.files)
    form = request.form
    overrides = {
        "mode": parse_choice(form, "mode", MODES),
        "top_k": parse_int(form, "top_k", minimum=1, maximum=rt.settings.max_top_k),
        "min_confidence": parse_float(form, "min_confidence", minimum=0.0, maximum=1.0),
        "min_aligned_hashes": parse_int(form, "min_aligned_hashes", minimum=1),
        "min_peak_ratio": parse_float(form, "min_peak_ratio", minimum=0.0),
        "max_occurrences_per_track": parse_int(form, "max_occurrences", minimum=1, maximum=500),
    }

    suffix = extension_of(upload.filename) or ".bin"
    fd, tmp_path = tempfile.mkstemp(prefix="audiofp-query-", suffix=suffix)
    os.close(fd)
    try:
        upload.save(tmp_path)
        result = rt.search_file(tmp_path, filename=upload.filename, **overrides)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:  # pragma: no cover
            pass
    return jsonify(format_search(result))
