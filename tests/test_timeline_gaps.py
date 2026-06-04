from src.x_browser_monitor import find_timeline_gaps
from src.x_types import Tweet


def test_find_timeline_gaps_detects_hole():
    tweets = [
        Tweet(id="1", text="a", created_at="2026-06-03T17:49:51.000Z"),
        Tweet(id="2", text="b", created_at="2026-06-03T20:59:31.000Z"),
    ]
    gaps = find_timeline_gaps(tweets, min_gap_minutes=45)
    assert len(gaps) == 1
    assert gaps[0]["gap_minutes"] == "189"
