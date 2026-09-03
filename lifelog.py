#!/usr/bin/env python3
"""Print a human-readable report for a 400-byte Casio life-log record."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

RECORD_SIZE = 400
EMPTY_U16 = 0xFFFE
EMPTY_U32 = 0xFFFFFFFE
TIMESTAMP_OFFSET = 0
CURRENT_DISTANCE_OFFSET = 246
DAILY_SUMMARIES_OFFSET = 318
CURRENT_STEPS_OFFSET = 374
CURRENT_DISTANCE_TOTAL_OFFSET = 378
PENDING_INTENSITY_OFFSET = 382
PENDING_DISTANCE_OFFSET = 392
BCD_TOTAL_OFFSET = 396
HISTORY_SLOTS = 24
INTENSITY_BUCKETS = 5


def u16(data: bytes, offset: int) -> int:
    """Read a little-endian unsigned 16-bit integer."""
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    """Read a little-endian unsigned 32-bit integer."""
    return struct.unpack_from("<I", data, offset)[0]


def read_intensity(data: bytes, offset: int) -> tuple[int, int, int, int, int]:
    """Read the five intensity buckets of a 10-byte front record."""
    return tuple(u16(data, offset + 2 * item) for item in range(INTENSITY_BUCKETS))


def bcd(byte: int) -> int:
    """Decode one packed-BCD byte."""
    high, low = divmod(byte, 16)
    if high > 9 or low > 9:
        raise ValueError(f"invalid BCD byte 0x{byte:02x}")
    return high * 10 + low


def decode_timestamp(data: bytes) -> dt.datetime:
    """Decode the six-byte timestamp at the start of a record."""
    values = [bcd(byte) for byte in data[:6]]
    return dt.datetime(2000 + values[0], *values[1:])


def decode_bcd_total(data: bytes) -> int:
    """Decode the four-byte little-endian packed-BCD total at offset 396."""
    return sum(bcd(data[BCD_TOTAL_OFFSET + index]) * 100**index for index in range(4))


def read_input(argument: str) -> tuple[str, tuple[str, ...], bytes]:
    """Read a log file or a hexadecimal record supplied on the command line."""
    path = Path(argument)
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
        annotations = tuple(
            line.lstrip()[1:].strip()
            for line in lines
            if line.lstrip().startswith("#")
        )
        hexadecimal = "".join(
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        )
        label = str(path)
    else:
        hexadecimal = argument
        label = "command line"
        annotations = ()

    try:
        data = bytes.fromhex(hexadecimal)
    except ValueError:
        try:
            data = zlib.decompress(base64.b64decode(hexadecimal))
        except Exception as error:
            raise ValueError(f"{label}: invalid hexadecimal or base64 data") from error
    if len(data) != RECORD_SIZE:
        raise ValueError(f"{label}: expected {RECORD_SIZE} bytes, got {len(data)}")
    return label, annotations, data


@dataclass(frozen=True)
class Activity:
    """One committed hour or the current, uncommitted hour."""

    hour: dt.datetime
    intensity: tuple[int, int, int, int, int]

    @property
    def steps(self) -> int:
        """Return the sum of all five step-intensity buckets."""
        return sum(value for value in self.intensity if value != EMPTY_U16)


@dataclass(frozen=True)
class HistoryEntry:
    """One previous-day history slot; None means that component was overwritten."""

    time: dt.time
    first: int | None
    second: int | None

    @property
    def known_steps(self) -> int:
        """Return the portion of the slot that is still readable."""
        return (self.first or 0) + (self.second or 0)


@dataclass(frozen=True)
class DailySummary:
    """One dated entry from the daily-summary ring."""

    days_ago: int
    steps: int
    distance: int

@dataclass(frozen=True)
class PreviousDay:
    """Previous-day detail that has not yet been overwritten."""

    date: dt.date
    total_steps: int
    total_distance: int
    front_records: tuple[Activity, ...]
    history: tuple[HistoryEntry, ...]
    overwritten_first: int
    overwritten_second: int

    @property
    def recovered_steps(self) -> int:
        """Return all previous-day steps that remain readable."""
        front = sum(activity.steps for activity in self.front_records)
        history = sum(entry.known_steps for entry in self.history)
        return front + history

    @property
    def unoverwritten(self) -> bool:
        """Return whether no previous-day component array was overwritten.

        Note: this is a *structural* property. A non-peek sync can clear the
        data without overwriting it, so this may be True even when the day's
        steps are semantically incomplete.
        """
        return self.overwritten_first == 0 and self.overwritten_second == 0


@dataclass(frozen=True)
class Field:
    """A half-open byte range whose meaning is known."""

    start: int
    end: int
    name: str


@dataclass(frozen=True)
class Entry:
    """One lifelog entry (committed hour, history slot, or pending walk)."""

    timestamp: dt.datetime
    steps: int
    intensity: tuple[int, ...]  # all buckets, empty marker (0xFFFE) → 0
    pending: bool = False
    summary: bool = False


@dataclass(frozen=True)
class Lifelog:
    """The confidently decoded portions of a life-log record."""

    timestamp: dt.datetime
    raw: bytes
    total_steps: int
    total_distance: int
    bcd_total: int
    activities: tuple[Activity, ...]
    pending_intensity: tuple[int, int, int]
    committed_distances: tuple[int, ...]
    pending_distance: int
    auxiliary_offset: int
    auxiliary_a: tuple[int, ...]
    auxiliary_b: tuple[int, ...]
    daily_summaries: tuple[DailySummary, ...]
    today_history: tuple[HistoryEntry, ...]
    warnings: tuple[str, ...]
    wiped: bool
    previous_day: PreviousDay | None

    @classmethod
    def parse(cls, data: bytes) -> Lifelog:
        """Parse a validated 400-byte record."""
        if len(data) != RECORD_SIZE:
            raise ValueError(f"expected {RECORD_SIZE} bytes, got {len(data)}")
        warnings = []

        timestamp = decode_timestamp(data)
        total_steps = u32(data, CURRENT_STEPS_OFFSET)
        pending = tuple(
            u16(data, PENDING_INTENSITY_OFFSET + 2 * index)
            for index in range(3)
        )
        pending_steps = sum(clean(pending))
        record_end, warning = find_record_end(
            data, timestamp, total_steps, pending_steps
        )
        if warning:
            warnings.append(warning)

        activities = []
        for index, offset in enumerate(range(6, record_end, 10)):
            hour = timestamp.replace(minute=0, second=0, microsecond=0)
            hour -= dt.timedelta(hours=index + 1)
            activities.append(Activity(hour, read_intensity(data, offset)))

        distance = u32(data, CURRENT_DISTANCE_TOTAL_OFFSET)
        pending_distance = u32(data, PENDING_DISTANCE_OFFSET)

        auxiliary_a = tuple(
            u16(data, record_end + 2 * index) for index in range(HISTORY_SLOTS)
        )
        auxiliary_b = tuple(
            u16(data, record_end + 50 + 2 * index) for index in range(HISTORY_SLOTS)
        )

        # Non-peek sync clears the hourly breakdown (front records + M1/M2) but
        # leaves the live daily totals at @374/@378 intact. Detect this so we
        # don't flag the empty components as a corruption mismatch.
        wiped = (
            total_steps > 0
            and not any(activity.steps for activity in activities)
            and pending_steps == 0
            and sum(clean(auxiliary_a)) + sum(clean(auxiliary_b)) == 0
        )

        distances, warning = distance_prefix(data, distance - pending_distance)
        if warning and not wiped:
            warnings.append(warning)

        # In the transitional hour (18:00–18:30) the front records still span
        # the whole day and M1/M2 holds only leftover residue, not today's day
        # history. Detect that so we don't mislabel the residue as today's data.
        transitional = (
            sum(activity.steps for activity in activities) + pending_steps
            == total_steps
        )

        today_history: tuple[HistoryEntry, ...] = ()
        if timestamp.hour >= 18 and not transitional:
            today_history = tuple(
                HistoryEntry(
                    history_time(i),
                    auxiliary_a[i] if auxiliary_a[i] != EMPTY_U16 else 0,
                    auxiliary_b[i] if auxiliary_b[i] != EMPTY_U16 else 0,
                )
                for i in range(HISTORY_SLOTS)
            )

        summaries, summary_warnings = read_daily_summaries(data)
        warnings.extend(summary_warnings)

        previous_day = parse_previous_day(
            data,
            timestamp,
            record_end,
            summaries,
        )

        if (
            previous_day is not None
            and previous_day.unoverwritten
            and previous_day.recovered_steps != previous_day.total_steps
        ):
            warnings.append(
                "previous day section intact but only "
                f"{previous_day.recovered_steps:,} of "
                f"{previous_day.total_steps:,} steps present "
                "(likely cleared by a prior non-peek sync)"
            )

        bcd_total = decode_bcd_total(data)
        if bcd_total != total_steps:
            warnings.append(
                f"BCD total {bcd_total:,} does not match steps {total_steps:,}"
            )
        reconstructed = sum(activity.steps for activity in activities) + pending_steps
        if timestamp.hour >= 18 and reconstructed != total_steps:
            # Add M1/M2 only when it is the populated day history; in the
            # transitional hour (18:00–18:30) front + pending already accounts
            # for everything and M1/M2 is just leftover residue.
            reconstructed += sum(clean(auxiliary_a)) + sum(clean(auxiliary_b))
        if reconstructed != total_steps:
            if wiped:
                warnings.append(
                    "non-peek sync cleared hourly breakdown (live totals persist)"
                )
            else:
                warnings.append(
                    f"step components reconstruct {reconstructed:,}, "
                    f"expected {total_steps:,}"
                )

        return cls(
            timestamp=timestamp,
            raw=data,
            total_steps=total_steps,
            total_distance=distance,
            bcd_total=bcd_total,
            activities=tuple(activities),
            pending_intensity=pending,
            committed_distances=distances,
            pending_distance=pending_distance,
            auxiliary_offset=record_end,
            auxiliary_a=auxiliary_a,
            auxiliary_b=auxiliary_b,
            daily_summaries=summaries,
            today_history=today_history,
            warnings=tuple(warnings),
            wiped=wiped,
            previous_day=previous_day,
        )

    def lifelog_entries(self) -> list[Entry]:
        """Return sorted lifelog entries from the already-parsed fields."""
        entries: list[Entry] = []

        for activity in self.activities:
            if activity.steps:
                ts = activity.hour.replace(minute=0)
                entries.append(Entry(ts, activity.steps, clean(activity.intensity)))

        pending = clean(self.pending_intensity)
        if sum(pending):
            ts = self.timestamp.replace(minute=0, second=0, microsecond=0)
            entries.append(Entry(ts, sum(pending), pending, pending=True))

        if self.timestamp.hour >= 18:
            for hist in self.today_history:
                if hist.known_steps:
                    ts = dt.datetime.combine(
                        self.timestamp.date(), hist.time
                    )
                    entries.append(Entry(ts, hist.known_steps, ()))

        if self.previous_day is not None:
            prev = self.previous_day
            for activity in prev.front_records:
                if activity.steps:
                    ts = activity.hour.replace(minute=0)
                    entries.append(Entry(ts, activity.steps, clean(activity.intensity)))
            for hist in prev.history:
                if hist.known_steps:
                    ts = dt.datetime.combine(prev.date, hist.time)
                    entries.append(Entry(ts, hist.known_steps, ()))

        for summary in self.daily_summaries:
            if summary.days_ago == 1 and self.previous_day is not None:
                continue  # yesterday is covered by previous_day recovery
            date = self.timestamp.date() - dt.timedelta(days=summary.days_ago)
            ts = dt.datetime.combine(date, dt.time(23, 59, 59))
            entries.append(Entry(ts, summary.steps, (), summary=True))

        entries.sort(key=lambda e: e.timestamp)
        return entries


def format_entry(entry: Entry) -> str:
    """Render one entry as a canonical single-line `lifelog ...` string."""
    parts = [
        f"lifelog time=\"{entry.timestamp:%Y-%m-%d %H:%M:%S}\"",
        f"steps=\"{entry.steps}\"",
    ]
    if entry.intensity:
        buckets = ",".join(map(str, entry.intensity))
        parts.append(f"intensity=\"{buckets}\"")
    if entry.pending:
        parts.append("pending")
    if entry.summary:
        parts.append("summary")
    return " ".join(parts)


def read_daily_summaries(
    data: bytes,
) -> tuple[tuple[DailySummary, ...], tuple[str, ...]]:
    """Decode daily summaries without collapsing empty date slots."""
    summaries = []
    warnings = []
    slot_count = (CURRENT_STEPS_OFFSET - DAILY_SUMMARIES_OFFSET) // 8
    for index in range(slot_count):
        offset = DAILY_SUMMARIES_OFFSET + 8 * index
        steps = u32(data, offset)
        distance = u32(data, offset + 4)
        steps_empty = steps == EMPTY_U32
        distance_empty = distance == EMPTY_U32
        if steps_empty and distance_empty:
            continue
        if steps_empty != distance_empty:
            warnings.append(f"daily summary slot {index + 1} is only half empty")
            continue
        summaries.append(DailySummary(index + 1, steps, distance))
    return tuple(summaries), tuple(warnings)


def parse_previous_day(
    data: bytes,
    timestamp: dt.datetime,
    record_end: int,
    summaries: tuple[DailySummary, ...],
) -> PreviousDay | None:
    """Recover the previous-day detail that precedes the overwrite boundary."""
    if timestamp.hour >= 18:
        return None
    summary = next((item for item in summaries if item.days_ago == 1), None)
    if summary is None:
        return None

    previous_date = timestamp.date() - dt.timedelta(days=1)
    # Previous-day front records: 13 records, newest first, spanning 23:00 down
    # to 11:00. They sit contiguously at off..off+130, immediately before M1.
    front_records = []
    for index in range(13):
        offset = record_end + 10 * index
        if offset + 10 > CURRENT_DISTANCE_OFFSET:
            continue  # overwritten by today's distance stack
        hour = dt.datetime.combine(previous_date, dt.time(23 - index))
        front_records.append(Activity(hour, read_intensity(data, offset)))

    first_start = record_end + 130
    second_start = record_end + 180
    safe_first = max(
        0, min(HISTORY_SLOTS, (CURRENT_DISTANCE_OFFSET - first_start) // 2)
    )
    safe_second = max(
        0, min(HISTORY_SLOTS, (CURRENT_DISTANCE_OFFSET - second_start) // 2)
    )
    history = []
    for index in range(HISTORY_SLOTS):
        first = u16(data, first_start + 2 * index) if index < safe_first else None
        second = u16(data, second_start + 2 * index) if index < safe_second else None
        if first == EMPTY_U16:
            first = 0
        if second == EMPTY_U16:
            second = 0
        time = history_time(index)
        history.append(HistoryEntry(time, first, second))

    total_steps = summary.steps
    total_distance = summary.distance
    return PreviousDay(
        date=previous_date,
        total_steps=total_steps,
        total_distance=total_distance,
        front_records=tuple(front_records),
        history=tuple(history),
        overwritten_first=24 - safe_first,
        overwritten_second=24 - safe_second,
    )


def _front_sum(data: bytes, start: int, end: int) -> int:
    """Sum steps in the front-record region as structured 10-byte records."""
    return sum(
        value
        for offset in range(start, end, 10)
        for value in read_intensity(data, offset)
        if value != EMPTY_U16
    )


def _history_sum(data: bytes, record_end: int) -> int:
    """Sum non-empty slots across both M1 and M2 history arrays."""
    total = 0
    for base in (record_end, record_end + 50):
        for index in range(HISTORY_SLOTS):
            value = u16(data, base + 2 * index)
            if value != EMPTY_U16:
                total += value
    return total


def _score_candidate(
    data: bytes, timestamp: dt.datetime, candidate: int, target: int
) -> int:
    """Return |target - reconstruction| for a candidate boundary."""
    front = _front_sum(data, 6, candidate)
    if timestamp.hour >= 18:
        # Two layouts are possible around the day-history transition (~18:30):
        #  - normal-day: front records (evening) + populated M1/M2 == target
        #  - transitional: front records (full day) == target, M1/M2 empty
        with_history = abs(target - front - _history_sum(data, candidate))
        without_history = abs(target - front)
        return min(with_history, without_history)
    return abs(target - front)


def find_record_end(
    data: bytes, timestamp: dt.datetime, total: int, pending: int
) -> tuple[int, str | None]:
    """Locate hourly records and warn when the boundary is not proven."""
    committed_target = total - pending

    if timestamp.hour < 18:
        expected = 6 + 10 * max(0, timestamp.hour - 6)
        if _front_sum(data, 6, expected) + pending == total:
            return expected, None

    # Candidate boundaries: front records + M1 (48B) + gap (2B) + M2 (48B)
    # must fit before the distance stack at @246, so at most 14 front records.
    scored = [
        (candidate, _score_candidate(data, timestamp, candidate, committed_target))
        for candidate in range(6, 150, 10)
    ]
    best_score = min(score for _, score in scored)
    best = [candidate for candidate, score in scored if score == best_score]

    # Prefer the transitional interpretation (front records alone equal the
    # target) only during the 18:00 hour, before the day history fills at
    # ~18:30. After that, the normal-day layout (front + populated M1/M2)
    # applies and "front == target" is mere aliasing.
    transitional = (
        [c for c in best if _front_sum(data, 6, c) == committed_target]
        if timestamp.hour == 18
        else []
    )
    chosen = min(transitional) if transitional else min(best)

    if best_score:
        warning = (
            f"record boundary @{chosen} is heuristic; "
            f"best reconciliation differs by {best_score:,} steps"
        )
        return chosen, warning

    if len(best) > 1:
        choices = ", ".join(str(offset) for offset in best)
        return chosen, f"record boundary is ambiguous among offsets {choices}"
    return chosen, None


def distance_prefix(
    data: bytes, target: int
) -> tuple[tuple[int, ...], str | None]:
    """Return an exact distance prefix, or no claimed fields and a warning."""
    if target < 0:
        return (), f"pending distance exceeds total distance by {-target:,} m"
    if target == 0:
        return (), None

    values = []
    total = 0
    for offset in range(
        CURRENT_DISTANCE_OFFSET, DAILY_SUMMARIES_OFFSET, 2
    ):
        value = u16(data, offset)
        values.append(value)
        total += value
        if total == target:
            return tuple(values), None
        if total > target:
            break
    return (), f"distance components do not reconcile to {target:,} m"


def clean(values: tuple[int, ...]) -> tuple[int, ...]:
    """Replace the protocol's empty marker with zero for display and sums."""
    return tuple(0 if value == EMPTY_U16 else value for value in values)


def history_time(index: int) -> dt.time:
    """Map a component-array index to its tentative half-hour slot."""
    slot = (index + 10) % HISTORY_SLOTS
    value = dt.datetime.min + dt.timedelta(hours=6, minutes=30 * slot)
    return value.time()



def known_fields(log: Lifelog) -> tuple[Field, ...]:
    """List every byte range currently understood by the parser."""
    end = log.auxiliary_offset
    fields = [
        Field(TIMESTAMP_OFFSET, 6, "timestamp"),
        Field(6, end, "front records (intensity)"),
        Field(end, end + 48, "M1 walking"),
        Field(end + 48, end + 50, "padding"),
        Field(end + 50, end + 98, "M2 running"),
        Field(
            CURRENT_DISTANCE_OFFSET,
            CURRENT_DISTANCE_OFFSET + 2 * len(log.committed_distances),
            "today's distance stack",
        ),
        Field(
            CURRENT_DISTANCE_OFFSET + 2 * len(log.committed_distances),
            DAILY_SUMMARIES_OFFSET - 4,
            "distance stack overflow (yesterday's in rollover)",
        ),
        Field(DAILY_SUMMARIES_OFFSET - 4, DAILY_SUMMARIES_OFFSET, "zero padding"),
        Field(DAILY_SUMMARIES_OFFSET, CURRENT_STEPS_OFFSET, "daily summaries"),
        Field(CURRENT_STEPS_OFFSET, PENDING_INTENSITY_OFFSET, "current totals"),
        Field(
            PENDING_INTENSITY_OFFSET,
            PENDING_INTENSITY_OFFSET + 6,
            "pending intensity",
        ),
        Field(PENDING_INTENSITY_OFFSET + 6, PENDING_DISTANCE_OFFSET, "zero padding"),
        Field(PENDING_DISTANCE_OFFSET, BCD_TOTAL_OFFSET, "pending distance"),
        Field(BCD_TOTAL_OFFSET, RECORD_SIZE, "BCD step total"),
    ]

    if log.previous_day is not None:
        previous = log.previous_day
        safe_first = HISTORY_SLOTS - previous.overwritten_first
        safe_second = HISTORY_SLOTS - previous.overwritten_second
        fields.extend(
            (
                Field(end, end + 130, "yesterday front intensity"),
                Field(end + 130, end + 130 + 2 * safe_first, "yesterday M1 walking"),
                Field(end + 178, end + 180, "padding"),
                Field(end + 180, end + 180 + 2 * safe_second, "yesterday M2 running"),
            )
        )

    return tuple(field for field in fields if field.start < field.end)


def unknown_fields(log: Lifelog) -> tuple[Field, ...]:
    """Return the complement of known_fields(); this is the sole unknown registry."""
    claimed = [False] * RECORD_SIZE
    for field in known_fields(log):
        for offset in range(max(0, field.start), min(RECORD_SIZE, field.end)):
            claimed[offset] = True

    unknown = []
    start = None
    for offset, is_claimed in enumerate((*claimed, True)):
        if not is_claimed and start is None:
            start = offset
        elif is_claimed and start is not None:
            unknown.append(Field(start, offset, "unknown"))
            start = None
    return tuple(unknown)


def print_unknown(log: Lifelog) -> None:
    """Print undecoded ranges in hexadecimal and little-endian integer form."""
    fields = unknown_fields(log)
    print("\n  Unknown fields")
    if not fields:
        print("    none")
        return

    for field in fields:
        raw = log.raw[field.start : field.end]
        print(f"    @{field.start:03d}-{field.end - 1:03d} ({len(raw)} bytes)")
        for offset in range(0, len(raw), 16):
            chunk = raw[offset : offset + 16]
            print(f"      hex: {' '.join(f'{byte:02x}' for byte in chunk)}")
        if field.start % 2 == 0 and len(raw) % 2 == 0:
            values = struct.unpack(f"<{len(raw) // 2}H", raw)
            for offset in range(0, len(values), 8):
                chunk = ", ".join(str(value) for value in values[offset : offset + 8])
                print(f"      u16: {chunk}")


def print_previous_day(previous: PreviousDay) -> None:
    """Render preserved previous-day entries and their recovery status."""
    front_records = [activity for activity in previous.front_records if activity.steps]
    recovered = previous.recovered_steps
    complete = previous.unoverwritten and recovered == previous.total_steps

    print(f"\n  Previous day detail ({previous.date})")
    print(
        f"    Stored total: {previous.total_steps:,} steps, "
        f"{previous.total_distance:,} m"
    )
    status = "complete" if complete else "partial"
    print(f"    Recovered:    {recovered:,}/{previous.total_steps:,} steps ({status})")
    if not complete:
        if previous.overwritten_first or previous.overwritten_second:
            print(
                "    Overwritten:  "
                f"{previous.overwritten_first} first-component slots, "
                f"{previous.overwritten_second} second-component slots"
            )
        else:
            print("    Missing:      likely cleared by a prior non-peek sync")

    for entry in sorted(previous.history, key=lambda item: item.time):
        if not entry.known_steps:
            continue
        exact = entry.first is not None and entry.second is not None
        first = "overwritten" if entry.first is None else str(entry.first)
        second = "overwritten" if entry.second is None else str(entry.second)
        steps = (
            f"{entry.known_steps:,}"
            if exact
            else f"at least {entry.known_steps:,}"
        )
        print(
            f"    {entry.time:%H:%M}  {steps} steps  "
            f"components=({first}, {second})"
        )

    for activity in sorted(front_records, key=lambda item: item.hour):
        print(
            f"    {activity.hour:%H:00}  {activity.steps:>5,} steps  "
            f"intensity={clean(activity.intensity)}"
        )


def print_current_activity(log: Lifelog) -> None:
    """Render committed and pending activity for the current day."""
    active = [activity for activity in reversed(log.activities) if activity.steps]
    print("\n  Hourly activity (five intensity buckets, lowest to highest)")
    if not active:
        print("    none committed")
    for activity in active:
        print(
            f"    {activity.hour:%H:00}  {activity.steps:>5,} steps  "
            f"intensity={clean(activity.intensity)}"
        )

    pending_steps = sum(clean(log.pending_intensity))
    if pending_steps:
        print(
            f"    {log.timestamp:%H:00}  {pending_steps:>5,} steps  "
            f"intensity={clean(log.pending_intensity)}  [pending]"
        )


def print_distance(log: Lifelog) -> None:
    """Render current-day distance components."""
    print("\n  Distance components (metres, newest first)")
    print(f"    committed={log.committed_distances or 'none'}")
    print(f"    pending={log.pending_distance:,}")


def print_auxiliary_history(log: Lifelog) -> None:
    """Render the current-day M1/M2 history (or tentative auxiliary arrays)."""
    history_is_current = log.timestamp.hour >= 18
    if history_is_current:
        entries = [
            (entry.time, entry.first, entry.second)
            for entry in log.today_history
            if entry.known_steps
        ]
    else:
        if log.previous_day is not None:
            return
        entries = []
        for index, (first, second) in enumerate(
            zip(log.auxiliary_a, log.auxiliary_b)
        ):
            first = 0 if first == EMPTY_U16 else first
            second = 0 if second == EMPTY_U16 else second
            if first or second:
                entries.append((history_time(index), first, second))

    if not entries:
        return
    heading = "Current-day history" if history_is_current else "Auxiliary history"
    note = (
        "30-minute mapping"
        if history_is_current
        else "tentative; not in today's total"
    )
    print(f"\n  {heading} @{log.auxiliary_offset} ({note})")
    for time, first, second in entries:
        print(
            f"    {time:%H:%M}  {first + second:>5,}  "
            f"components=({first}, {second})"
        )


def print_daily_summaries(log: Lifelog) -> None:
    """Render dated summary-ring entries not expanded as previous-day detail."""
    summaries = tuple(
        summary
        for summary in log.daily_summaries
        if log.previous_day is None or summary.days_ago != 1
    )
    if not summaries:
        return

    print("\n  Earlier daily summaries")
    for summary in summaries:
        date = log.timestamp.date() - dt.timedelta(days=summary.days_ago)
        print(
            f"    {date}: {summary.steps:,} steps, "
            f"{summary.distance:,} m"
        )


def print_report(
    label: str, annotations: tuple[str, ...], log: Lifelog
) -> None:
    """Render a parsed record for a human reader."""
    committed_steps = sum(activity.steps for activity in log.activities)
    pending_steps = sum(clean(log.pending_intensity))
    history_steps = sum(clean(log.auxiliary_a)) + sum(clean(log.auxiliary_b))
    history_is_current = log.timestamp.hour >= 18
    step_check = committed_steps + pending_steps
    if history_is_current and step_check != log.total_steps:
        step_check += history_steps
    distance_check = sum(log.committed_distances) + log.pending_distance

    print(label)
    if annotations:
        print("  Notes:")
        for annotation in annotations:
            print(f"    {annotation}")
    print(f"  Captured:  {log.timestamp:%Y-%m-%d %H:%M:%S}")
    print(f"  Steps:     {log.total_steps:,}")
    print(f"  Distance:  {log.total_distance:,} m")
    print(
        "  Integrity: "
        f"steps {step_check:,}/{log.total_steps:,} "
        f"({'wiped' if log.wiped else 'OK' if step_check == log.total_steps else 'MISMATCH'}), "
        f"distance {distance_check:,}/{log.total_distance:,} "
        f"({'wiped' if log.wiped else 'OK' if distance_check == log.total_distance else 'MISMATCH'}), "
        f"BCD {'OK' if log.bcd_total == log.total_steps else 'MISMATCH'}"
    )

    if log.warnings:
        print("  Warnings:")
        for warning in log.warnings:
            print(f"    - {warning}")

    print_current_activity(log)
    print_distance(log)
    if log.previous_day is not None:
        print_previous_day(log.previous_day)
    print_auxiliary_history(log)
    print_daily_summaries(log)
    print_unknown(log)


def main() -> None:
    """Parse command-line inputs and print reports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input", nargs="+", help="log file or 400-byte hexadecimal record"
    )
    arguments = parser.parse_args()

    for index, argument in enumerate(arguments.input):
        if index:
            print()
        try:
            label, annotations, data = read_input(argument)
            print_report(label, annotations, Lifelog.parse(data))
        except (OSError, ValueError) as error:
            parser.error(str(error))


if __name__ == "__main__":
    main()
