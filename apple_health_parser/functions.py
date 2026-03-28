from datetime import datetime

def years_and_weeks_between(start: datetime, end: datetime):
    if end < start:
        start, end = end, start

    # Step 1: count full years
    years = end.year - start.year

    anniversary = start.replace(year=start.year + years)

    # If we haven't reached the anniversary yet, subtract one year
    if anniversary > end:
        years -= 1
        anniversary = start.replace(year=start.year + years)

    # Step 2: weeks from the remaining period
    remaining_days = (end - anniversary).days
    weeks = remaining_days // 7

    return years, weeks

def get_line(enddate:datetime):
    y,w = years_and_weeks_between(datetime(2026,2,2),enddate)
    start_line = 19
    line_modulo = 14
    day_line = (y*365+w*7)//7*line_modulo+start_line \
                    + enddate.date().isoweekday() - 1
    return day_line