from __future__ import annotations


def format_time(seconds: float) -> str:
    """Format seconds into a human-readable string."""
    total_seconds = max(0, round(seconds))
    if total_seconds < 60:
        return f"{total_seconds}s"

    minutes, secs = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"
