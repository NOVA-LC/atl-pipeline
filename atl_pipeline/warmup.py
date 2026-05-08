"""Daily send-volume cap based on warmup day.

Conservative ramp: starts at 5/day for a week, then doubles every ~5 days. This
protects gonenova.com's sender reputation. Once Resend reports >95% delivery and
<2% bounce on 3 consecutive days, we can edit the schedule to ramp faster.
"""
import datetime, os
from pathlib import Path

# Day-of-pipeline (1-indexed) → max sends that day.
# Tyler has an established sender — starting at 50 and holding.
SCHEDULE = {
    1:  50,
    2:  50,
    3:  50,
    4:  50,
    5:  50,
    6:  50,
    7:  50,
}
DEFAULT_AFTER_DAY_21 = 50

# WARMUP_START_FILE lives on the persistent volume so day-counter survives container restarts.
START_FILE = Path(os.environ.get('PIPELINE_DB_PATH', 'atl_pipeline.db')).parent / '.warmup_start'

def get_start_date():
    """The date Tyler first ran the pipeline. Used to compute day-of-pipeline."""
    if START_FILE.exists():
        return datetime.date.fromisoformat(START_FILE.read_text().strip())
    today = datetime.date.today()
    START_FILE.parent.mkdir(parents=True, exist_ok=True)
    START_FILE.write_text(today.isoformat())
    return today

def day_of_pipeline():
    start = get_start_date()
    return (datetime.date.today() - start).days + 1

def todays_max_sends():
    d = day_of_pipeline()
    if d in SCHEDULE:
        return SCHEDULE[d]
    return DEFAULT_AFTER_DAY_21

def status():
    d = day_of_pipeline()
    cap = todays_max_sends()
    return {'day': d, 'cap_today': cap, 'cap_tomorrow': SCHEDULE.get(d+1, DEFAULT_AFTER_DAY_21)}
