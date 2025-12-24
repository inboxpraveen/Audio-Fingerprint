"""API endpoint definitions."""

import os
import time
import tempfile
from flask import Blueprint, request, current_app, jsonify
from werkzeug.utils import secure_filename

from ..core import load_audio, preprocess_audio, Fingerprinter, generate_hashes, match_fingerprint
from .validators import validate_audio_file
from .responses import format_search_response, format_error_response


api_bp = Blueprint('api', __name__)


@api_bp.route('/search', methods=['POST'])
def search():
    """
    Search for a song by audio query.
    
    Request: multipart/form-data with 'audio' file
    Response: JSON with matches and metadata
    """
    start_time = time.time()
    
    try:
        # Validate request
        if 'audio' not in request.files:
            return format_error_response('No audio file provided', 400)
        
        audio_file = request.files['audio']
        
        # Validate file
        is_valid, error_msg = validate_audio_file(
            audio_file,
            current_app.config.get('ALLOWED_EXTENSIONS'),
            current_app.config.get('MAX_CONTENT_LENGTH')
        )
        if not is_valid:
            return format_error_response(error_msg, 400)
        
        # Save temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(audio_file.filename)[1]) as tmp_file:
            audio_file.save(tmp_file.name)
            tmp_filepath = tmp_file.name
        
        try:
            # Load and process audio
            audio, sr = load_audio(tmp_filepath, sr=current_app.config.get('SAMPLE_RATE', 11025))
            audio = preprocess_audio(audio)
            
            # Generate fingerprint
            fingerprinter = Fingerprinter(
                sr=sr,
                n_fft=current_app.config.get('N_FFT', 2048),
                hop_length=current_app.config.get('HOP_LENGTH', 512),
                peak_neighborhood_size=current_app.config.get('PEAK_NEIGHBORHOOD_SIZE', 20),
                min_amplitude=current_app.config.get('MIN_AMPLITUDE', 10)
            )
            peaks = fingerprinter.generate_fingerprint(audio)
            
            # Generate hashes
            query_hashes = generate_hashes(
                peaks,
                song_id=None,
                fan_value=current_app.config.get('FAN_VALUE', 5)
            )
            
            # Match against database
            matches = match_fingerprint(query_hashes, current_app.storage, top_k=5)
            
            # Calculate processing time
            processing_time_ms = (time.time() - start_time) * 1000
            
            # Format response
            return format_search_response(matches, processing_time_ms, len(audio) / sr)
        
        finally:
            # Clean up temp file
            if os.path.exists(tmp_filepath):
                os.unlink(tmp_filepath)
    
    except Exception as e:
        current_app.logger.error(f"Search error: {str(e)}")
        return format_error_response(f"Search failed: {str(e)}", 500)


@api_bp.route('/songs', methods=['GET'])
def get_songs():
    """Get list of all indexed songs."""
    try:
        songs = current_app.storage.get_all_songs()
        return jsonify({
            'songs': songs,
            'count': len(songs)
        })
    except Exception as e:
        current_app.logger.error(f"Get songs error: {str(e)}")
        return format_error_response(str(e), 500)


@api_bp.route('/songs/<song_id>', methods=['GET'])
def get_song(song_id):
    """Get metadata for a specific song."""
    try:
        metadata = current_app.storage.get_song_metadata(song_id)
        if metadata is None:
            return format_error_response('Song not found', 404)
        return jsonify(metadata)
    except Exception as e:
        current_app.logger.error(f"Get song error: {str(e)}")
        return format_error_response(str(e), 500)


@api_bp.route('/index', methods=['POST'])
def index_songs():
    """
    Start indexing songs from a directory.
    
    Body: {directory_path: "..."}
    """
    try:
        data = request.get_json()
        directory_path = data.get('directory_path')
        
        if not directory_path:
            return format_error_response('directory_path is required', 400)
        
        if not os.path.isdir(directory_path):
            return format_error_response('Invalid directory path', 400)
        
        # TODO: Implement async indexing job
        return jsonify({
            'message': 'Indexing job started',
            'directory': directory_path
        }), 202
    
    except Exception as e:
        current_app.logger.error(f"Index error: {str(e)}")
        return format_error_response(str(e), 500)


@api_bp.route('/stats', methods=['GET'])
def get_stats():
    """Get database statistics."""
    try:
        stats = current_app.storage.get_stats()
        return jsonify(stats)
    except Exception as e:
        current_app.logger.error(f"Stats error: {str(e)}")
        return format_error_response(str(e), 500)


@api_bp.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'timestamp': time.time()
    })

