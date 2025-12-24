"""Response formatting utilities."""

from flask import jsonify


def format_search_response(matches, processing_time_ms, query_duration_sec):
    """
    Format search response.
    
    Args:
        matches: List of (song_id, confidence_score, metadata) tuples
        processing_time_ms: Processing time in milliseconds
        query_duration_sec: Query audio duration in seconds
    
    Returns:
        Flask JSON response
    """
    formatted_matches = []
    
    for song_id, confidence, metadata in matches:
        match_data = {
            'song_id': song_id,
            'confidence': round(confidence, 4),
        }
        
        # Add metadata fields
        if metadata:
            match_data.update({
                'title': metadata.get('title', 'Unknown'),
                'artist': metadata.get('artist', 'Unknown'),
                'duration': metadata.get('duration'),
                'filepath': metadata.get('filepath'),
            })
        
        formatted_matches.append(match_data)
    
    response = {
        'matches': formatted_matches,
        'query_duration_sec': round(query_duration_sec, 2),
        'processing_time_ms': round(processing_time_ms, 2),
        'found': len(formatted_matches) > 0
    }
    
    return jsonify(response)


def format_error_response(error_message, status_code=400):
    """
    Format error response.
    
    Args:
        error_message: Error message string
        status_code: HTTP status code
    
    Returns:
        Flask JSON response with status code
    """
    return jsonify({
        'error': error_message,
        'status': status_code
    }), status_code

