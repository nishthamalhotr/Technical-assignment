"""Employee Attendance & Analytics API."""

import os
import re
import logging
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, AsyncGenerator, Literal
from contextlib import asynccontextmanager

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

logger = logging.getLogger(__name__)
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
MAX_EPOCH_MS = 4_102_444_800_000
INDEXES_READY = False

def create_indexes() -> None:
    global INDEXES_READY
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_joined",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_joined_department",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )
    INDEXES_READY = True


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    try:
        create_indexes()
    except PyMongoError:
        logger.exception("MongoDB is unavailable; startup index creation will retry on /health")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def parse_date(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label} must be a valid YYYY-MM-DD date") from None
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{label} must be a valid YYYY-MM-DD date")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(422, "month must use YYYY-MM format")
    year, month = map(int, value.split("-"))
    if year == 0:
        raise HTTPException(422, "month must use a valid calendar year")
    last = monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def epoch_to_datetime(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    try:
        instant = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "timestamp is out of range") from None
    return instant.replace(microsecond=0)


def truncate_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(normalized.timestamp() * 1000)


def parse_clock(value: str) -> time:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise HTTPException(422, "shift times must use HH:MM in 24-hour format")
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def shift_is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_clock(shift_end) <= parse_clock(shift_start)


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = truncate_seconds(punch_in).astimezone(IST)
    day = local.date()
    if shift_is_overnight(shift_start, shift_end) and local.time().replace(tzinfo=None) < parse_clock(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_instant(punch_in: datetime, shift_start: str, attendance_date_value: date | None = None) -> datetime:
    local = truncate_seconds(punch_in).astimezone(IST)
    start_day = attendance_date_value or local.date()
    hour, minute = map(int, shift_start.split(":"))
    return datetime.combine(start_day, time(hour, minute), tzinfo=IST)


def shift_end_instant(shift_start: str, shift_end: str, attendance_date_value: date) -> datetime:
    end_day = attendance_date_value
    if shift_is_overnight(shift_start, shift_end):
        end_day += timedelta(days=1)
    hour, minute = map(int, shift_end.split(":"))
    return datetime.combine(end_day, time(hour, minute), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date_value: date | None = None) -> int:
    local = truncate_seconds(punch_in).astimezone(IST)
    day = attendance_date_value or local.date()
    start = shift_start_instant(local, shift_start, day)
    elapsed = int((local - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, attendance_date_value: date
) -> int:
    end = shift_end_instant(shift_start, shift_end, attendance_date_value).astimezone(UTC)
    elapsed = int((truncate_seconds(punch_out) - end).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def half_up(value: float | int | Decimal, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def attendance_response(doc: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            converted = dict(change)
            if field in ("punch_in", "punch_out"):
                converted["from"] = to_epoch_ms(converted.get("from"))
                converted["to"] = to_epoch_ms(converted.get("to"))
            changes[field] = converted
        history.append(
            {
                "at": to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def employee_response(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[StrictStr, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    email: Annotated[StrictStr, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[StrictStr, Field(min_length=1, max_length=50)]
    shift_start: Annotated[StrictStr, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[StrictStr, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: StrictStr

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: StrictInt | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_null_timestamp(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("punched_at cannot be null")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: StrictStr
    punched_at: StrictInt | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_null_timestamp(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("punched_at cannot be null")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: StrictInt | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)
    punch_out: StrictInt | None = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH_MS)
    reason: Annotated[StrictStr, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[StrictStr, Field(min_length=1, max_length=50)]

    @model_validator(mode="before")
    @classmethod
    def reject_null_mutable_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for field in ("status", "punch_in", "punch_out"):
                if field in value and value[field] is None:
                    raise ValueError(f"{field} cannot be null")
        return value


def workdays_inclusive(start: date, end: date) -> int:
    if end < start:
        return 0
    count = (end - start).days + 1
    full_weeks, remainder = divmod(count, 7)
    weekday = start.weekday()
    first_weekdays = min(remainder, max(0, 5 - weekday))
    after_weekend = max(0, remainder - (7 - weekday))
    return full_weeks * 5 + first_weekdays + min(after_weekend, 5)


def month_log_pipeline(
    emp_code: str,
    start: date,
    end: date,
    joined_on: date | None = None,
) -> list[dict[str, Any]]:
    present_conditions: list[dict[str, Any]] = [
        {"$in": ["$status", list(PRESENCE_STATUSES)]},
        {"$gte": ["$_weekday", 2]},
        {"$lte": ["$_weekday", 6]},
    ]
    if joined_on is not None:
        present_conditions.append({"$gte": ["$date", joined_on.isoformat()]})
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$addFields": {
                "_weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": present_conditions},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def attendance_filter(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        dates: dict[str, str] = {}
        if date_from:
            dates["$gte"] = date_from
        if date_to:
            dates["$lte"] = date_to
        query["date"] = dates
    if status:
        query["status"] = status
    return query


def department_summary_pipeline(start: date, end: date, department: str | None) -> list[dict[str, Any]]:
    match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
                "employee_join_dates": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes", "join_dates": "$employee_join_dates"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_joined_on": {
                                "$arrayElemAt": [
                                    {
                                        "$map": {
                                            "input": {
                                                "$filter": {
                                                    "input": "$$join_dates",
                                                    "as": "employee",
                                                    "cond": {"$eq": ["$$employee.emp_code", "$emp_code"]},
                                                }
                                            },
                                            "as": "employee",
                                            "in": "$$employee.joined_on",
                                        }
                                    },
                                    0,
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "format": "%Y-%m-%d",
                                        "timezone": "UTC",
                                    }
                                }
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$gte": ["$date", "$_joined_on"]},
                                                {"$gte": ["$_weekday", 2]},
                                                {"$lte": ["$_weekday", 6]},
                                            ]
                                        },
                                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "metrics",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {"$ifNull": [{"$arrayElemAt": ["$metrics.present_days", 0]}, 0]},
                "work_hours_sum": {"$ifNull": [{"$arrayElemAt": ["$metrics.work_hours_sum", 0]}, 0]},
                "work_hours_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.work_hours_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.late_count", 0]}, 0]},
                "total_late_minutes": {"$ifNull": [{"$arrayElemAt": ["$metrics.total_late_minutes", 0]}, 0]},
                "leave_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.leave_count", 0]}, 0]},
                "on_duty_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.on_duty_count", 0]}, 0]},
            }
        },
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(start: date, end: date, department: str | None, limit: int) -> list[dict[str, Any]]:
    employee_pipeline: list[dict[str, Any]] = [{"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}}]
    if department is not None:
        employee_pipeline.append({"$match": {"department": department}})
    employee_pipeline.append({"$project": {"_id": 0, "name": 1, "department": 1}})
    return [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"code": "$_id"},
                "pipeline": employee_pipeline,
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


def trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    day_count = (end - start).days + 1
    return [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            }
        },
        {
            "$set": {
                "dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": start.isoformat(),
                                                "format": "%Y-%m-%d",
                                                "timezone": "UTC",
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                                "format": "%Y-%m-%d",
                                "timezone": "UTC",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$dates"},
        {
            "$set": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {"$lte": ["$$employee.joined_on", "$dates"]},
                        }
                    }
                },
                "weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$dates",
                            "format": "%Y-%m-%d",
                            "timezone": "UTC",
                        }
                    }
                },
                "emp_codes": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$employees",
                                "as": "employee",
                                "cond": {"$lte": ["$$employee.joined_on", "$dates"]},
                            }
                        },
                        "as": "employee",
                        "in": "$$employee.emp_code",
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"target_date": "$dates", "codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$target_date"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "metrics",
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$metrics.late_count", 0]}, 0]},
                "is_working_day": {"$and": [{"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dates": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$dates",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
        if not INDEXES_READY:
            create_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_response(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    day = attendance_day(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if candidate is None:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    punch_in_at = truncate_seconds(candidate["punch_in"])
    elapsed = int((punched_at - punch_in_at).total_seconds())
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    day = parse_date(candidate["date"])
    updates = {
        "punch_out": punched_at,
        "work_hours": compute_work_hours(punch_in_at, punched_at),
        "overtime_minutes": compute_overtime(
            punched_at, employee["shift_start"], employee["shift_end"], day
        ),
    }
    updates["half_day"] = updates["work_hours"] < 4.5
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_response(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
) -> dict[str, Any]:
    parse_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")
    fields_set = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & fields_set):
        raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")

    old_values = {
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    final_status = body.status if "status" in fields_set else old_values["status"]
    if final_status in ("ABSENT", "LEAVE"):
        final_in = final_out = None
    else:
        final_in = epoch_to_datetime(body.punch_in) if "punch_in" in fields_set else old_values["punch_in"]
        final_out = epoch_to_datetime(body.punch_out) if "punch_out" in fields_set else old_values["punch_out"]
        if final_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(final_in, employee["shift_start"], employee["shift_end"]).isoformat() != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if final_out is not None:
            elapsed = int((final_out - final_in).total_seconds())
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if final_status in ("ABSENT", "LEAVE"):
        new_values = {
            "status": final_status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        new_work_hours = compute_work_hours(final_in, final_out) if final_out is not None else None
        new_values = {
            "status": final_status,
            "punch_in": final_in,
            "punch_out": final_out,
            "work_hours": new_work_hours,
            "late_minutes": compute_late_minutes(final_in, employee["shift_start"], parse_date(date)),
            "overtime_minutes": (
                compute_overtime(final_out, employee["shift_start"], employee["shift_end"], parse_date(date))
                if final_out is not None
                else 0
            ),
            "half_day": new_work_hours < 4.5 if new_work_hours is not None else False,
        }

    changes: dict[str, dict[str, Any]] = {}
    for field, new_value in new_values.items():
        old_value = old_values[field]
        if old_value != new_value:
            changes[field] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "regularization does not change the record")

    history_entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_filter: dict[str, Any] = (
        {"history": current["history"]} if "history" in current else {"history": {"$exists": False}}
    )
    update_filter = {"_id": current["_id"], **history_filter}
    set_values = {key: value for key, value in new_values.items()}
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": set_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed; retry regularization")
    return attendance_response(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 1, "joined_on": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")
    start, end = parse_month(month)
    joined_on = parse_date(employee["joined_on"], "joined_on")
    working_start = max(start, joined_on)
    working_days = workdays_inclusive(working_start, end) if working_start <= end else 0
    result = list(
        db.attendance_logs.aggregate(
            month_log_pipeline(emp_code, start, end, joined_on),
            allowDiskUse=True,
        )
    )
    metrics = result[0] if result else {}
    present_days = half_up(metrics.get("present_days", 0))
    attendance_pct = half_up(Decimal(str(present_days)) * 100 / working_days, 4) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics.get("leave_days", 0),
        "late_count": metrics.get("late_count", 0),
        "total_late_minutes": metrics.get("total_late_minutes", 0),
        "total_overtime_minutes": metrics.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: str | None = None,
) -> dict[str, Any]:
    start, end = parse_month(month)
    result = db.employees.aggregate(
        department_summary_pipeline(start, end, department),
        allowDiskUse=True,
    )
    items = []
    for row in result:
        if row["headcount"] == 0:
            continue
        work_count = row.pop("work_hours_count", 0)
        work_sum = row.pop("work_hours_sum", 0)
        row["present_days"] = half_up(row.get("present_days", 0))
        row["avg_work_hours"] = (
            half_up(Decimal(str(work_sum)) / Decimal(work_count)) if work_count else None
        )
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
) -> dict[str, Any]:
    start, end = parse_month(month)
    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(start, end, department, limit),
            allowDiskUse=True,
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[str, Query(alias="from")],
    to_date: Annotated[str, Query(alias="to")],
) -> dict[str, Any]:
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "trend range must be ordered and contain at most 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    rows = list(db.employees.aggregate(trend_pipeline(department, start, end), allowDiskUse=True))
    for row in rows:
        row["attendance_rate"] = half_up(row["attendance_rate"], 4) if row["attendance_rate"] is not None else None
        row["moving_avg_7d"] = half_up(row["moving_avg_7d"], 4) if row["moving_avg_7d"] is not None else None
        row["present_count"] = half_up(row["present_count"])
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query = attendance_filter(emp_code, date_from, date_to, status)
        command: dict[str, Any] = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        explained = db.command("explain", command, verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint in ("employee_monthly", "late_leaderboard"):
        if month is None:
            raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required")
            explained_employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
            joined_on = parse_date(explained_employee["joined_on"], "joined_on") if explained_employee else None
            pipeline = month_log_pipeline(emp_code, start, end, joined_on)
        else:
            pipeline = leaderboard_pipeline(start, end, department, limit)
        explained = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        start, end = parse_month(month)
        pipeline = department_summary_pipeline(start, end, department)
        explained = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(422, "trend range must be ordered and contain at most 92 days")
        pipeline = trend_pipeline(department, start, end)
        explained = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json_util.loads(json_util.dumps(explained, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
