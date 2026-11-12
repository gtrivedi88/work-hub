"""Read calendars from iCalendar and export local tasks as calendar events."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .sources import fetch, local_data
from .store import CLOSED, Store, TrackerError, local_today, parse_date, timestamp, web_url


def add_calendar(store: Store, name: str, source: str):
    target = web_url(source) if "://" in source else str(Path(source).expanduser().resolve())
    with store.db(write=True) as db:
        store.set(db, "calendar", name, {"source": target})


def date_value(value: str, parameters: dict | None = None) -> dt.date | dt.datetime:
    if re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value, "%Y%m%d").date()
    parameters = parameters or {}
    value = value.removesuffix("Z")
    parsed = dt.datetime.strptime(value, "%Y%m%dT%H%M%S" if len(value) == 15 else "%Y%m%dT%H%M")
    if parameters.get("utc"):
        return parsed.replace(tzinfo=dt.timezone.utc)
    if parameters.get("TZID"):
        return parsed.replace(tzinfo=ZoneInfo(parameters["TZID"].strip('"')))
    return parsed.astimezone()


def date_property(value: str, parameters: dict) -> dt.date | dt.datetime:
    return date_value(value, {**parameters, "utc": value.endswith("Z")})


def unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], value)


def blocks(data: bytes) -> list[dict]:
    lines = re.sub(r"\r?\n[ \t]", "", data.decode("utf-8-sig")).splitlines()
    if not any(line.upper() == "BEGIN:VCALENDAR" for line in lines):
        raise TrackerError("The source did not return an iCalendar document.")
    event = None
    events = []
    level = 0
    for line in lines:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key.upper() == "BEGIN":
            if value.upper() == "VEVENT" and event is None:
                event, level = {}, 0
            elif event is not None:
                level += 1
            continue
        if key.upper() == "END" and event is not None:
            if level:
                level -= 1
            else:
                events.append(event)
                event = None
            continue
        if event is not None and level == 0:
            name, *options = key.split(";")
            params = dict(part.split("=", 1) for part in options if "=" in part)
            event.setdefault(name.upper(), []).append((value, params))
    return events


def occurrence_days(start: dt.date, finish: dt.date, rules: dict):
    freq = rules.get("FREQ")
    interval = int(rules.get("INTERVAL", 1))
    count = int(rules["COUNT"]) if "COUNT" in rules else None
    if interval < 1 or (count is not None and count < 1):
        raise TrackerError("Invalid recurrence interval or count.")
    if not freq:
        yield start
        return
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") or set(rules) - {
        "FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"
    }:
        raise TrackerError("Unsupported calendar recurrence rule.")
    byday = rules.get("BYDAY", "").split(",") if "BYDAY" in rules else []
    weekdays = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
    if byday and (freq not in ("DAILY", "WEEKLY") or set(byday) - set(weekdays)):
        raise TrackerError("Unsupported calendar recurrence days.")
    if rules.get("WKST", "MO") not in weekdays:
        raise TrackerError("Unsupported calendar week start.")
    week_start = weekdays.index(rules.get("WKST", "MO"))
    origin = start - dt.timedelta(days=(start.weekday() - week_start) % 7)
    found = 0
    for offset in range((finish - start).days + 1):
        day = start + dt.timedelta(days=offset)
        months = (day.year - start.year) * 12 + day.month - start.month
        week = (day - origin).days // 7
        matches = {
            "DAILY": offset % interval == 0 and (not byday or weekdays[day.weekday()] in byday),
            "WEEKLY": week % interval == 0 and weekdays[day.weekday()] in (byday or [weekdays[start.weekday()]]),
            "MONTHLY": months % interval == 0 and day.day == start.day,
            "YEARLY": months % (interval * 12) == 0 and day.day == start.day,
        }[freq]
        if matches or day == start:
            if count is not None and found >= count:
                break
            found += 1
            yield day


def read_calendar(data: bytes, first: dt.date, last: dt.date) -> tuple[list[dict], list[str]]:
    events = blocks(data)
    overrides = {}
    for event in events:
        if event.get("UID") and event.get("RECURRENCE-ID"):
            uid = event["UID"][0][0]
            overrides.setdefault(uid, []).append(event["RECURRENCE-ID"][0])
    results, warnings = [], []
    for event in events:
        def field(name, default=""):
            return event.get(name, [(default, {})])[0]

        title = unescape(field("SUMMARY", "(untitled event)")[0])
        if field("STATUS")[0] == "CANCELLED" or not field("DTSTART")[0]:
            continue
        try:
            start = date_property(*field("DTSTART"))
            timed = isinstance(start, dt.datetime)
            day = start.date() if timed else start
            end = date_property(*field("DTEND")) if field("DTEND")[0] else (
                start if timed else start + dt.timedelta(days=1)
            )
            duration = end - start
            rule = dict(part.split("=", 1) for part in field("RRULE")[0].split(";") if "=" in part)
            until = date_value(rule["UNTIL"], {"utc": rule["UNTIL"].endswith("Z")}) if "UNTIL" in rule else None
            exceptions = {date_property(value, params) for text, params in event.get("EXDATE", [])
                          for value in text.split(",")}
            uid = field("UID")[0]
            if uid and not event.get("RECURRENCE-ID"):
                exceptions.update(date_property(*value) for value in overrides.get(uid, []))
            if day > last + dt.timedelta(days=1):
                continue
            try:
                occurrences = list(occurrence_days(day, last + dt.timedelta(days=1), rule))
            except TrackerError as exc:
                warnings.append(f"{title[:100]}: {exc}")
                occurrences = [day]
            starts = [start + (occurrence - day) for occurrence in occurrences]
            starts.extend(date_property(value, params) for text, params in event.get("RDATE", [])
                          for value in text.split(","))
            for instance in set(starts) - exceptions:
                if until and type(instance) is type(until) and instance > until:
                    continue
                start_at = instance.astimezone() if timed else instance
                finish_at = (instance + duration).astimezone() if timed else instance + duration
                first_day = start_at.date() if timed else start_at
                last_day = finish_at.date() if timed else finish_at - dt.timedelta(days=1)
                if first_day <= last and last_day >= first:
                    results.append({
                        "title": title, "start": start_at.isoformat(), "end": finish_at.isoformat(),
                        "all_day": not timed, "location": unescape(field("LOCATION")[0]),
                    })
        except (ValueError, OverflowError, TypeError, ZoneInfoNotFoundError, TrackerError) as exc:
            warnings.append(f"{title[:100]}: {exc}")
    return sorted(results, key=lambda event: event["start"]), warnings


def agenda(store: Store, start: str | None = None, days: int = 7) -> dict:
    first = parse_date(start) if start else local_today()
    if not 1 <= days <= 90:
        raise TrackerError("Agenda range must be 1–90 days.")
    last = first + dt.timedelta(days=days - 1)
    calendars, errors, items = [], {}, []
    with store.db() as db:
        for item in store.all(db):
            if item["status"] in CLOSED:
                continue
            planned = item["scheduled"]
            date = dt.datetime.fromisoformat(planned).astimezone().date().isoformat() if planned else item["due"]
            if date and first.isoformat() <= date <= last.isoformat():
                items.append({**item, "date": date})
    for name, calendar_source in store.settings("calendar").items():
        source = calendar_source["source"]
        try:
            data = fetch(source) if "://" in source else local_data(source)
            events, warnings = read_calendar(data, first, last)
            calendars.extend({**event, "calendar": name} for event in events)
            if warnings:
                errors[name] = warnings
        except (TrackerError, ValueError, OSError) as exc:
            errors[name] = [str(exc)]
    return {"start": first.isoformat(), "end": last.isoformat(),
            "items": sorted(items, key=lambda item: item["date"]),
            "calendar_events": sorted(calendars, key=lambda event: event["start"]),
            "calendar_errors": errors}


def calendar_export(store: Store, items: list[dict]) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")

    stamp = dt.datetime.fromisoformat(timestamp()).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Work Hub//Tracker//EN"]
    for item in items:
        if item["status"] in CLOSED or not (item["scheduled"] or item["due"]):
            continue
        lines.extend(("BEGIN:VEVENT", f"UID:{item['id']}.{store.workspace}@work-hub",
                      f"DTSTAMP:{stamp}", f"SUMMARY:{escape(item['title'])}"))
        if item["scheduled"]:
            begin = dt.datetime.fromisoformat(item["scheduled"])
            end = begin + dt.timedelta(minutes=item["estimate_minutes"] or 30)
            lines.extend((f"DTSTART:{begin:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"))
        else:
            day = parse_date(item["due"])
            lines.extend((f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                          f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}"))
        if item["next_action"]:
            lines.append("DESCRIPTION:" + escape(item["next_action"]))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
