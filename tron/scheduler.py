import calendar
import datetime
import logging
import re

from collections import deque
from tron.groctimespecification import IntervalTimeSpecification, SpecificTimeSpecification
from tron.utils import timeutils

log = logging.getLogger('tron.scheduler')

WEEK = 'mtwrfsu'

# Also support Monday, Mon, mon, mo, Tuesday, Tue, tue, tu...
CONVERT_DAYS = dict()       # day name/abbrev => {mtwrfsu}
CONVERT_DAYS_INT = dict()   # day name/abbrev => {0123456}
for day_list in (calendar.day_name,
                 calendar.day_abbr,
                 WEEK,
                 ('mo', 'tu', 'we', 'th', 'fr', 'sa', 'su')):
    for key, value in zip(day_list, range(7)):
        CONVERT_DAYS_INT[key] = value
        CONVERT_DAYS_INT[key.lower()] = value
        CONVERT_DAYS_INT[key.upper()] = value
        CONVERT_DAYS[key] = WEEK[value]
        CONVERT_DAYS[key.lower()] = WEEK[value]
        CONVERT_DAYS[key.upper()] = value

# Support January, Jan, january, jan, February, Feb...
CONVERT_MONTHS = dict()     # month name/abbrev => {0 <= k <= 11}
# calendar stores month data with a useless element in front. cut it off.
for month_list in (calendar.month_name[1:], calendar.month_abbr[1:]):
    for key, value in zip(month_list, range(1, 13)):
        CONVERT_MONTHS[key] = value
        CONVERT_MONTHS[key.lower()] = value

# Build a regular expression that matches this:
# ("every"|ordinal) (days) ["of|in" (monthspec)] (["at"] time)
# Where:
# ordinal specifies a comma separated list of "1st" and so forth
# days specifies a comma separated list of days of the week (for example,
#   "mon", "tuesday", with both short and long forms being accepted); "every
#   day" is equivalent to "every mon,tue,wed,thu,fri,sat,sun"
# monthspec specifies a comma separated list of month names (for example,
#   "jan", "march", "sep"). If omitted, implies every month. You can also say
#   "month" to mean every month, as in "1,8,15,22 of month 09:00".
# time specifies the time of day, as HH:MM in 24 hour time.
# from http://code.google.com/appengine/docs/python/config/cron.html#The_Schedule_Format

DAY_VALUES = '|'.join(CONVERT_DAYS.keys() + ['day'])
MONTH_VALUES = '|'.join(CONVERT_MONTHS.keys() + ['month'])
DATE_SUFFIXES = 'st|nd|rd|th'

MONTH_DAYS_EXPR = '(?P<month_days>every|((\d+(%s),?)+))?' % DATE_SUFFIXES
DAYS_EXPR = r'((?P<days>((%s),?)+))?' % DAY_VALUES
MONTHS_EXPR = r'((in|of) (?P<months>((%s),?)+))?' % MONTH_VALUES
TIME_EXPR = r'((at )?(?P<time>\d\d:\d\d))?'

GROC_SCHEDULE_EXPR = ''.join([
    r'^',
    MONTH_DAYS_EXPR, r' ?',
    DAYS_EXPR, r' ?',
    MONTHS_EXPR, r' ?',
     TIME_EXPR, r' ?',
    r'$'
])

GROC_SCHEDULE_RE = re.compile(GROC_SCHEDULE_EXPR)

class ConstantScheduler(object):
    """The constant scheduler only schedules the first one.  The job run starts then next when finished"""
    def next_runs(self, job):
        if job.next_to_finish():
            return []
        
        job_runs = job.build_runs()
        for job_run in job_runs:
            job_run.set_run_time(timeutils.current_time())
        
        return job_runs

    def job_setup(self, job):
        job.constant = True
        job.queueing = False

    def __str__(self):
        return "CONSTANT"

    def __eq__(self, other):
        return isinstance(other, ConstantScheduler)

    def __ne__(self, other):
        return not self == other


class GrocScheduler(object):
    """Wrapper around SpecificTimeSpecification in the Google App Engine cron library"""
    def __init__(self, ordinals=None, weekdays=None, months=None, monthdays=None,
                 timestr='00:00', timezone=None, start_time=None):
        """Parameters:
          timestr   - the time of day to run, as 'HH:MM'
          ordinals  - first, second, third &c, as a set of integers in 1..5 to be
                      used with "1st <weekday>", etc.
          monthdays - set of integers to be used with "<month> 3rd", etc.
          months    - the months that this should run, as a set of integers in 1..12
          weekdays  - the days of the week that this should run, as a set of integers,
                      0=Sunday, 6=Saturday
          timezone  - the optional timezone as a string for this specification.
                      Defaults to UTC - valid entries are things like Australia/Victoria
                      or PST8PDT.
          start_time - Backward-compatible parameter for DailyScheduler
        """
        self.ordinals = ordinals
        self.weekdays = weekdays
        self.months = months
        self.monthdays = monthdays
        self.timestr = timestr
        self.timezone = timezone
        self._start_time = start_time
        self.string_repr = 'every day of month'

        self._time_spec = None

    @property
    def time_spec(self):
        if self._time_spec is None:
            self._time_spec = SpecificTimeSpecification(ordinals=self.ordinals,
                                                        weekdays=self.weekdays,
                                                        months=self.months,
                                                        monthdays=self.monthdays,
                                                        timestr=self.timestr,
                                                        timezone=self.timezone)
        return self._time_spec

    def parse(self, scheduler_str):
        """Parse a schedule string."""
        self.string_repr = scheduler_str

        def parse_number(day):
            return int(''.join(c for c in day if c.isdigit()))

        m = GROC_SCHEDULE_RE.match(scheduler_str.lower())

        if m.group('time') is None:
            if self._start_time is None:
                self.timestr = '00:00'
            else:
                self.timestr = '%02d:%02d' % (self._start_time.hour,
                                              self._start_time.minute)
        else:
            self.timestr = m.group('time')

        if m.group('days') in (None, 'day'):
            self.weekdays = None
        else:
            self.weekdays = set(CONVERT_DAYS_INT[d] for d in m.group('days').split(','))

        self.monthdays = None
        self.ordinals = None
        if m.group('month_days') != 'every':
            values = set(parse_number(n) for n in m.group('month_days').split(','))
            if self.weekdays is None:
                self.monthdays = values
            else:
                self.ordinals = values

        if m.group('months') in (None, 'month'):
            self.months = None
        else:
            self.months = set(CONVERT_MONTHS[mo] for mo in m.group('months').split(','))

    def parse_legacy_days(self, days):
        """Parse a string that would have been passed to DailyScheduler"""
        self.weekdays = set(CONVERT_DAYS_INT[d] for d in days)
        if self.weekdays != set([0, 1, 2, 3, 4, 5, 6]):
            self.string_repr = 'every %s of month' % ','.join(days)

    def get_daily_waits(self, days):
        """Backwards compatibility with DailyScheduler"""
        self.parse_legacy_days(days)

    def _get_start_time(self):
        if self._start_time is None:
            hms = [int(val) for val in self.timestr.strip().split(':')]
            while len(hms) < 3:
                hms.append(0)
            hour, minute, second = hms
            return datetime.time(hour=hour, minute=minute, second=second)

    def _set_start_time(self, start_time):
        self._start_time = start_time

    start_time = property(_get_start_time, _set_start_time)

    def next_runs(self, job):
        # Find the next time to run
        if job.runs:
            start_time = job.runs[0].run_time
        else:
            start_time = timeutils.current_time()

        run_time = self.time_spec.GetMatch(start_time)

        job_runs = job.build_runs()
        for job_run in job_runs:
            job_run.set_run_time(run_time)

        return job_runs

    def job_setup(self, job):
        job.queueing = True

    def __str__(self):
        # Backward compatible string representation which also happens to be
        # user-friendly
        if self.string_repr == 'every day of month':
            return 'DAILY'
        else:
            return self.string_repr

    def __eq__(self, other):
        return isinstance(other, GrocScheduler) and \
           all(getattr(self, attr) == getattr(other, attr)
               for attr in ('ordinals',
                            'weekdays',
                            'months',
                            'monthdays',
                            'timestr',
                            'timezone'))

    def __ne__(self, other):
        return not self == other


# GrocScheduler can pretend to be a DailyScheduler in order to be backdward-
# compatible
DailyScheduler = GrocScheduler


class IntervalScheduler(object):
    """The interval scheduler runs a job (to success) based on a configured interval
    """
    def __init__(self, interval=None):
        self.interval = interval
    
    def next_runs(self, job):
        run_time = timeutils.current_time() + self.interval
        
        job_runs = job.build_runs()
        for job_run in job_runs:
            job_run.set_run_time(run_time)
        
        return job_runs

    def job_setup(self, job):
        job.queueing = False
    
    def __str__(self):
        return "INTERVAL:%s" % self.interval
        
    def __eq__(self, other):
        return isinstance(other, IntervalScheduler) and self.interval == other.interval
    
    def __ne__(self, other):
        return not self == other
