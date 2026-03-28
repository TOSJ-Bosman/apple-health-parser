from __future__ import annotations
from dataclasses import dataclass, field
import datetime
import xml.etree.ElementTree as ET
from typing import List, Sequence, Dict
from abc import ABC, abstractmethod
from collections import defaultdict
import gspread
from google.oauth2.service_account import Credentials
from apple_health_parser.functions import get_line
from argparse import ArgumentParser

Cell = gspread.cell.Cell

class user_settings:
    start_date = datetime.date(2026,2,2)
    start_line = 19
    line_modulo = 14

    def __init__(self):
        self.sy,self.sw,self.sdow = self.start_date.isocalendar()
        self.sw += 0

@dataclass
class Record:
    """Single entry from Apple Health XML."""
    type_code: str      # e.g. "HKQuantityTypeIdentifierStepCount"
    start: datetime
    end: datetime
    creation: datetime
    value: str
    unit: str

    @property
    def day(self):
        """Calendar day based on the start time."""
        return self.creation.date()
    

@dataclass
class Day:
    date: datetime.date
    metrics: Dict[str, DailyMetric] = field(default_factory=dict)

    def __getitem__(self, key: str):
        return self.metrics[key]

    def __setitem__(self, key: str, value: DailyMetric):
        self.metrics[key] = value

    def __contains__(self, key: str):
        return key in self.metrics

    def keys(self):
        return self.metrics.keys()

    def items(self):
        return self.metrics.items()

    def values(self):
        return self.metrics.values()

# ---------------------------------------------------------------------------
# DailyMetric: the final result for a metric on a specific day
# ---------------------------------------------------------------------------

@dataclass
class DailyMetric:
    """
    Final, computed metric value for a single day.
    Contains:
    - name: e.g. "Steps"
    - date: datetime.date
    - value: the computed number
    - records: the list of Record objects used in the computation
    """
    name: str
    date: datetime.date
    value: float | str
    records: List[Record]

    def safe_value(self):
        from math import isnan
        v = self.value
        if isinstance(v, float) and isnan(v):
            return ""
        return v

# ---------------------------------------------------------------------------
# DailyMetricDefinition: defines HOW a metric is computed
# ---------------------------------------------------------------------------

class DailyMetricDefinition(ABC):
    """
    Abstract base for a metric like "Steps", "Weight", "Avg Heart Rate".
    Each subclass:
      - creates an accumulator for each day
      - updates it record-by-record
      - finalizes a DailyMetric at the end of the day
    """

    def __init__(self, name: str, type_codes: Sequence[str]):
        self.name = name
        self.type_codes = list(type_codes)

    def matches(self, record: Record) -> bool:
        """Return True if this metric should include this record."""
        return record.type_code in self.type_codes
    
    # NEW: let metrics decide which day they belong to
    def get_effective_day(self, record: Record) -> datetime.date:
        """By default, use the record's calendar day."""
        return record.day

    # --- accumulator lifecycle ---------------------------------------------

    @abstractmethod
    def new_accumulator(self) -> dict:
        """Create a fresh accumulator for a new day."""
        ...

    @abstractmethod
    def update_accumulator(self, acc: dict, record: Record) -> None:
        """Update accumulator with a single record."""
        ...

    @abstractmethod
    def finalize(self, acc: dict, day: datetime.date) -> DailyMetric:
        """Convert accumulator to a DailyMetric object."""
        ...

DailyMetricList = list[DailyMetricDefinition]

class SumDailyMetric(DailyMetricDefinition):
    """
    Example: total step count or total active energy for a day.
    """

    def new_accumulator(self) -> dict:
        return {"sum": 0.0, "records": []}

    def update_accumulator(self, acc: dict, record: Record) -> None:
        acc["sum"] += float(record.value)
        acc["records"].append(record)

    def finalize(self, acc: dict, day: datetime.date) -> DailyMetric:
        return DailyMetric(
            name=self.name,
            date=day,
            value=acc["sum"],
            records=acc["records"],
        )
    
class LastValueDailyMetric(DailyMetricDefinition):
    """
    Example: body weight (take the last measurement of the day),
    or resting heart rate, or last glucose reading, etc.
    """

    def new_accumulator(self) -> dict:
        # store:
        # - last value seen
        # - last record (optional)
        # - records list if you want them
        return {
            "last": None,
            "records": [],
        }

    def update_accumulator(self, acc: dict, record: Record) -> None:
        # Always overwrite with the newest record encountered
        # (your records are already processed in chronological order in practice)
        acc["last"] = float(record.value)
        acc["records"].append(record)

    def finalize(self, acc: dict, day: datetime.date) -> DailyMetric:
        value = acc["last"]
        if value is None:
            value = float("nan")  # Empty day → no value

        return DailyMetric(
            name=self.name,
            date=day,
            value=value,
            records=acc["records"],
        )

class SleepBaseMetric(DailyMetricDefinition):
    """Base for sleep metrics using Apple Health sleep analysis records."""

    SLEEP_TYPE = "HKCategoryTypeIdentifierSleepAnalysis"

    def __init__(self, name: str):
        super().__init__(name=name, type_codes=[self.SLEEP_TYPE])

    def get_effective_day(self, record: Record) -> datetime.date:
        """
        If the sleep segment starts before 12:00 (noon),
        attribute it to the previous day (the 'night' before).
        Otherwise, use the calendar day of the start.
        """
        start = record.start  # datetime.datetime
        day = start.date()
        if start.time() < datetime.time(12, 0):
            return day - datetime.timedelta(days=1)
        return day

class SleepStageDurationMetric(SleepBaseMetric):
    """
    Metric for total duration (in hours) spent in a specific sleep stage per day.
    Example stages (Apple Health):
      - HKCategoryValueSleepAnalysisAsleepCore
      - HKCategoryValueSleepAnalysisAsleepDeep
      - HKCategoryValueSleepAnalysisAsleepREM
      - HKCategoryValueSleepAnalysisAwake
    """

    def __init__(self, name: str, stage_value: str):
        super().__init__(name=name)
        self.stage_value = stage_value

    def new_accumulator(self) -> dict:
        return {
            "seconds": 0.0,
            "records": [],
        }

    def update_accumulator(self, acc: dict, record: Record) -> None:
        # Only count this record if it matches the stage we care about
        if record.value != self.stage_value:
            return

        duration_seconds = (record.end - record.start).total_seconds()
        acc["seconds"] += duration_seconds
        acc["records"].append(record)

    def finalize(self, acc: dict, day: datetime.date) -> DailyMetric:
        total_seconds = int(acc["seconds"])
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        # Format as "Xh Ym"
        formatted = f"{hours:02d}:{minutes:02d}"

        return DailyMetric(
            name=self.name,
            date=day,
            value=formatted,
            records=acc["records"],
        )

class SleepBedtimeMetric(SleepBaseMetric):
    """
    Bedtime for each day:
    - defined as the start time of the FIRST non-awake sleep segment of that day
    - output format: "HH:MM" (24h)
    """

    AWAKE_VALUE = "HKCategoryValueSleepAnalysisAwake"

    def new_accumulator(self) -> dict:
        return {
            "bedtime": None,   # will store a datetime.datetime
            "records": [],
        }

    def update_accumulator(self, acc: dict, record: Record) -> None:
        # Ignore "awake" records
        if record.value == self.AWAKE_VALUE:
            return

        start = record.start  # datetime.datetime

        # If we don't have a bedtime yet, or this record starts earlier, take it
        if acc["bedtime"] is None or start < acc["bedtime"]:
            acc["bedtime"] = start

        acc["records"].append(record)

    def finalize(self, acc: dict, day: datetime.date) -> DailyMetric:
        bt = acc["bedtime"]

        if bt is None:
            # No sleep that day → empty string or "": choose what's best for your API
            formatted = ""
        else:
            # Format as "HH:MM" in 24h clock
            formatted = bt.strftime("%H:%M")

        return DailyMetric(
            name=self.name,
            date=day,
            value=formatted,
            records=acc["records"],
        )
    
class SleepTotalDurationMetric(SleepBaseMetric):
    """
    Total sleep time per day (all non-awake sleep stages together).
    Output format: "HH:MM" (zero-padded, 24h-style).
    """

    AWAKE_VALUE = "HKCategoryValueSleepAnalysisAwake"

    def new_accumulator(self) -> dict:
        return {
            "seconds": 0.0,
            "records": [],
        }

    def update_accumulator(self, acc: dict, record: Record) -> None:
        # Skip awake segments entirely
        if record.value == self.AWAKE_VALUE:
            return

        # Sum duration of all other sleep states (Core, Deep, REM, Unspecified, etc.)
        duration_seconds = (record.end - record.start).total_seconds()
        acc["seconds"] += duration_seconds
        acc["records"].append(record)

    def finalize(self, acc: dict, day: datetime.date) -> DailyMetric:
        total_seconds = int(acc["seconds"])
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        formatted = f"{hours:02d}:{minutes:02d}"  # "HH:MM"

        return DailyMetric(
            name=self.name,
            date=day,
            value=formatted,
            records=acc["records"],
        )

class SleepEffectivenessMetric(DailyMetricDefinition):
    """
    Sleep effectiveness = SleepTotal duration rounded to nearest hour.
    Example: 08:28 -> 8, 08:31 -> 9.
    """

    def __init__(self, total_sleep_metric_name: str = "SleepTotal"):
        # Does not read records → no type codes needed
        super().__init__(name="SleepEffectiveness", type_codes=[])
        self.total_sleep_metric_name = total_sleep_metric_name

    # These three are required by the base class, even though we don't use them:
    def new_accumulator(self) -> dict:
        return {}

    def update_accumulator(self, acc: dict, record: Record) -> None:
        pass  # No-op

    def finalize(self, acc: dict, day: datetime.date) -> DailyMetric:
        # We compute this metric during the week finalization step.
        # The real logic lives in a separate method below.
        raise RuntimeError("SleepEffectivenessMetric must be computed from existing metrics, not from records.")

    # Helper to compute effectiveness AFTER SleepTotal is available
    def compute_from_total(self, sleep_total_str: str, day: datetime.date) -> DailyMetric:
        """
        sleep_total_str is of form 'HH:MM'
        """
        if not sleep_total_str:
            # No sleep that day
            return DailyMetric(
                name=self.name,
                date=day,
                value="",
                records=[]
            )

        # Parse "HH:MM"
        hours, minutes = map(int, sleep_total_str.split(":"))

        # Round to nearest hour
        rounded = hours + (1 if minutes > 30 else 0)

        return DailyMetric(
            name=self.name,
            date=day,
            value=rounded,
            records=[],
        )

class Week:
    days: List["Day"] = field(default_factory=list)

    def __init__(self,year,number):
        self.year        = year
        self.week_number = number
        self.start_date  = datetime.date.fromisocalendar(year,number,1)
        self.end_date    = datetime.date.fromisocalendar(year,number,7)
        self.days: List[Day] = []  # <-- real list here

    def __iter__(self):
        return iter(self.days)

    def get_day(self,day_number):
        return datetime.date.fromisocalendar(self.year,self.week_number,day_number)

def get_date(strIn:str,mode="date"):
    """Converts creationDate from AppleHealth to a date/datetime object."""
    if mode == "date":
        return datetime.datetime.strptime(strIn,"%Y-%m-%d %H:%M:%S %z").date()
    elif mode == "datetime":
        return datetime.datetime.strptime(strIn,"%Y-%m-%d %H:%M:%S %z")
    elif mode =="time":
        return datetime.datetime.strptime(strIn,"%Y-%m-%d %H:%M:%S %z").time()
    
def cellAppend(cells:List,row,col,data):
    if data is not None:
        cells.append(Cell(row=row,col=col,value=data))

    return cells

def main(year,week_nbr):
    cw = Week(year,week_nbr)
    print(("Start date:",str(cw.start_date),"end date",str(cw.end_date)))

    xml_path = "data/apple_health_export/export.xml"
    source_name = {"Thomas’s Apple Watch"}

    # Define types
    STEP_TYPE = "HKQuantityTypeIdentifierStepCount"
    RHR_TYPE  = "HKQuantityTypeIdentifierRestingHeartRate"
    
    metric_defs: DailyMetricList = [
        SumDailyMetric(name="Steps", type_codes=[STEP_TYPE]),
        LastValueDailyMetric(name="RHR", type_codes=[RHR_TYPE]),
        # Sleep stages
        SleepStageDurationMetric(
            name="SleepCore_h",
            stage_value="HKCategoryValueSleepAnalysisAsleepCore"),
        SleepStageDurationMetric(
            name="SleepDeep_h",
            stage_value="HKCategoryValueSleepAnalysisAsleepDeep"),
        SleepStageDurationMetric(
            name="SleepREM_h",
            stage_value="HKCategoryValueSleepAnalysisAsleepREM"),
        SleepStageDurationMetric(
            name="SleepAwake_h",
            stage_value="HKCategoryValueSleepAnalysisAwake"),
        SleepBedtimeMetric(name="Bedtime"),
        SleepTotalDurationMetric(name="SleepTotal")
    ]

    wanted = {t for md in metric_defs for t in md.type_codes}

    # Parse xml
    tree = ET.parse(xml_path)
    root = tree.getroot()

    Reclist = list()

    # Pass over xml data first time
    for elem in root.findall(".//Record"):
        at = elem.attrib

        rec_type = at.get("type")
        if wanted is not None and rec_type not in wanted:
            continue

        if at.get("sourceName") not in source_name:
            continue

        rec_date = at.get("creationDate")

        if not get_date(rec_date) >= cw.start_date \
        or not get_date(rec_date) <= cw.end_date:
            continue

        val = at.get("value")
        if not type(val) is str:
            val = str(val)

        rec = Record(type_code=rec_type,
                     start      = get_date(at.get("startDate"),mode="datetime"),
                     end        = get_date(at.get("endDate"),mode="datetime"),
                     creation   = get_date(at.get("creationDate"),mode="datetime"),
                     value      = at.get("value"),
                     unit       = at.get("unit"),
                     )
        
        Reclist.append(rec)

    # Second pass
    day_accumulators: dict[datetime.date, dict[str, dict]] = defaultdict(dict)

    for rec in Reclist:
        for md in metric_defs:
            if not md.matches(rec):
                continue

            metric_accs_for_day = day_accumulators[rec.day]

            acc = metric_accs_for_day.get(md.name)
            if acc is None:
                acc = md.new_accumulator()
                metric_accs_for_day[md.name] = acc

            md.update_accumulator(acc, rec)

    # Finalize into a week
    for offset in range(1, 8):
        curr_date = cw.get_day(offset)

        acc_for_day = day_accumulators.get(curr_date, {})
        metrics_for_day: dict[str, DailyMetric] = {}

        for md in metric_defs:
            acc = acc_for_day.get(md.name, md.new_accumulator())
            metric = md.finalize(acc, curr_date)
            metrics_for_day[md.name] = metric

        day = Day(date=curr_date, metrics=metrics_for_day)
        cw.days.append(day)
        #print(curr_date, day.metrics["SleepDeep_h"].value)
        #print(curr_date, "Bedtime:", day.metrics["Bedtime"].value)
        #print(curr_date, "RHR:", day.metrics["RHR"].value)
        
    print("Data loaded for the desired week.")
    # Open user settings and write
    TOSJ = user_settings()

    SERVICE_ACCOUNT_FILE = ".credentials/auto-edit-bgcoaching-46fb7046b5ff.json"  # Update with your JSON file
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
    SHEET_ID = "15BJyU3c7OqHnA425891h4895rtUIwJ595Zr1HnkhHsA"

    # Authenticate and create the client
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)

    sheet = sh.get_worksheet(3)

    print("Logged into google sheet. Adding values.")
    
    cells = []

    for d in cw:
        day_line = get_line(datetime.datetime.combine(d.date,datetime.datetime.min.time()))
        print(day_line)
        cells = cellAppend(cells,day_line,7,d["RHR"].safe_value())
        cells = cellAppend(cells,day_line,21,d["Steps"].safe_value())
        cells = cellAppend(cells,day_line,24,7)
        cells = cellAppend(cells,day_line,25,1)
        cells = cellAppend(cells,day_line,28,"Non")
        cells = cellAppend(cells,day_line,29,3)
        cells = cellAppend(cells,day_line,30,3)
        cells = cellAppend(cells,day_line,31,"Aucun")
        cells = cellAppend(cells,day_line,32,d["Bedtime"].safe_value())
        cells = cellAppend(cells,day_line,33,d["SleepTotal"].safe_value())
        cells = cellAppend(cells,day_line,35,d["SleepDeep_h"].safe_value())
        cells = cellAppend(cells,day_line,36,d["SleepCore_h"].safe_value())
        cells = cellAppend(cells,day_line,37,d["SleepREM_h"].safe_value())
        print(d["Bedtime"].safe_value())
        print(type(d["Bedtime"].safe_value()))

    #print(cells)
    sheet.update_cells(cells,value_input_option="USER_ENTERED")
        
        


if __name__== "__main__":
    # Parse input arguments to import the right week
    parser = ArgumentParser()
    parser.add_argument("-year", default=datetime.datetime.now().year)
    parser.add_argument("-week", default=datetime.datetime.now().isocalendar().week - 1)
    args = parser.parse_args()
    year = int(args.year)
    week = int(args.week)

    cw = Week(year, week)
    # Display welcome message
    print(
        "Welcome to the BG-coaching importer.\nCurrent setting: Apple health importer."
    )
    print(
        f"Importing week {args.week}, {args.year}. Spanning {cw.start_date} to {cw.end_date}."
    )

    
    main(year,week)