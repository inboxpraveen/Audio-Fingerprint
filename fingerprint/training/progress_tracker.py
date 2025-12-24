"""Progress tracking for indexing operations."""

import time


class ProgressTracker:
    """Track progress of indexing operations."""
    
    def __init__(self, total):
        """
        Initialize progress tracker.
        
        Args:
            total: Total number of items
        """
        self.total = total
        self.current = 0
        self.start_time = time.time()
        self.last_update_time = self.start_time
    
    def update(self, item_name=None):
        """
        Update progress (increment by 1).
        
        Args:
            item_name: Name of current item (optional)
        """
        self.current += 1
        self.last_update_time = time.time()
    
    def get_progress(self):
        """
        Get current progress information.
        
        Returns:
            dict: Progress information
        """
        elapsed_time = time.time() - self.start_time
        progress_ratio = self.current / self.total if self.total > 0 else 0
        
        # Estimate remaining time
        if self.current > 0:
            avg_time_per_item = elapsed_time / self.current
            remaining_items = self.total - self.current
            estimated_remaining_time = avg_time_per_item * remaining_items
        else:
            estimated_remaining_time = 0
        
        return {
            'current': self.current,
            'total': self.total,
            'progress_percent': progress_ratio * 100,
            'elapsed_time_sec': elapsed_time,
            'estimated_remaining_time_sec': estimated_remaining_time,
            'items_per_sec': self.current / elapsed_time if elapsed_time > 0 else 0
        }
    
    def print_progress(self, item_name=None):
        """
        Print progress to console.
        
        Args:
            item_name: Name of current item (optional)
        """
        progress = self.get_progress()
        
        progress_bar = self._create_progress_bar(progress['progress_percent'])
        
        message = f"\r{progress_bar} {self.current}/{self.total} "
        message += f"({progress['progress_percent']:.1f}%) "
        message += f"[{progress['elapsed_time_sec']:.1f}s elapsed, "
        message += f"{progress['estimated_remaining_time_sec']:.1f}s remaining]"
        
        if item_name:
            message += f" - {item_name}"
        
        print(message, end='', flush=True)
        
        if self.current >= self.total:
            print()  # New line at completion
    
    def _create_progress_bar(self, percent, width=30):
        """
        Create ASCII progress bar.
        
        Args:
            percent: Progress percentage (0-100)
            width: Width of progress bar
        
        Returns:
            str: Progress bar string
        """
        filled = int(width * percent / 100)
        bar = '█' * filled + '░' * (width - filled)
        return f"[{bar}]"

