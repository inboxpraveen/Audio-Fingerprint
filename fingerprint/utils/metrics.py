"""Performance metrics collection."""

import time
from collections import defaultdict


class MetricsCollector:
    """Collect and track performance metrics."""
    
    def __init__(self):
        """Initialize metrics collector."""
        self.metrics = defaultdict(list)
        self.counters = defaultdict(int)
    
    def record_time(self, metric_name, duration):
        """
        Record a timing metric.
        
        Args:
            metric_name: Name of the metric
            duration: Duration in seconds
        """
        self.metrics[metric_name].append(duration)
    
    def increment_counter(self, counter_name, value=1):
        """
        Increment a counter.
        
        Args:
            counter_name: Name of the counter
            value: Increment value (default: 1)
        """
        self.counters[counter_name] += value
    
    def get_stats(self, metric_name):
        """
        Get statistics for a timing metric.
        
        Args:
            metric_name: Name of the metric
        
        Returns:
            dict: Statistics (mean, min, max, count)
        """
        values = self.metrics.get(metric_name, [])
        
        if not values:
            return {
                'count': 0,
                'mean': 0,
                'min': 0,
                'max': 0,
                'total': 0
            }
        
        return {
            'count': len(values),
            'mean': sum(values) / len(values),
            'min': min(values),
            'max': max(values),
            'total': sum(values)
        }
    
    def get_counter(self, counter_name):
        """
        Get counter value.
        
        Args:
            counter_name: Name of the counter
        
        Returns:
            int: Counter value
        """
        return self.counters.get(counter_name, 0)
    
    def get_all_metrics(self):
        """
        Get all metrics and counters.
        
        Returns:
            dict: All metrics data
        """
        result = {
            'timings': {},
            'counters': dict(self.counters)
        }
        
        for metric_name in self.metrics:
            result['timings'][metric_name] = self.get_stats(metric_name)
        
        return result
    
    def clear(self):
        """Clear all metrics."""
        self.metrics.clear()
        self.counters.clear()


class Timer:
    """Context manager for timing code blocks."""
    
    def __init__(self, metrics_collector=None, metric_name=None):
        """
        Initialize timer.
        
        Args:
            metrics_collector: Optional MetricsCollector instance
            metric_name: Optional metric name
        """
        self.metrics_collector = metrics_collector
        self.metric_name = metric_name
        self.start_time = None
        self.duration = None
    
    def __enter__(self):
        """Start timer."""
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop timer and record duration."""
        self.duration = time.time() - self.start_time
        
        if self.metrics_collector and self.metric_name:
            self.metrics_collector.record_time(self.metric_name, self.duration)
        
        return False

