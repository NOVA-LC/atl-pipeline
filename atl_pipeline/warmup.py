"""Daily send-volume cap based on warmup day.

Tyler chose to start at 30/day and ramp aggressively. Risk is on him — if Gmail
flags the domain, deliverability craters and recovery takes weeks.
"""
import datetime
from pathlib import Path

# Day-of-pipeline (1-indexed) → max sends that day
SCHEDULE = {
    1:  30,
    2:  30,
    3:  35,
    4:  35,
    5:  40,
    6:  40,   # weekend — usually less effective for B2B; consider pausing
    7:  40,
    8:  45,
    9:  45,
    10: 50,
    11: 50,
    12: 55,
    13: 55,
    14: 60,
}
DEFAULT_AFTER_DAY_14 = 60

# Recommended (more conservative — kept here for reference, not used):
RECOMMENDED_SCHEDULE = {
    1: 5, 2: 8, 3: 10, 4: 12, 5: 15, 6: 15, 7: 18,
    8: 20, 9: 22, 10: 25, 11: 25, 12: 28, 13: 30, 14: 32,
}

START_FILE = Path('.warmup_start')

def get_start_date():
    """The date Tyler first ran the pipeline. Used to compute day-of-pipeline."""
    if START_FILE.exists():
        return datetime.date.fromisoformat(START_FILE.read_text().strip())
    today = datetime.date.today()
    START_FILE.write_text(today.isoformat())
    return today

def day_of_pipeline():
    start = get_start_date()
    return (datetime.date.today() - start).days + 1

def todays_max_sends():
    d = day_of_pipeline()
    if d in SCHEDULE:
        return SCHEDULE[d]
    return DEFAULT_AFTER_DAY_14

def status():
    d = day_of_pipeline()
    cap = todays_max_sends()
    return {'day': d, 'cap_today': cap, 'cap_tomorrow': SCHEDULE.get(d+1, DEFAULT_AFTER_DAY_14)}
