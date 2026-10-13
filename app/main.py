"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index("department")
        db.employees.create_index("joined_on")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    except Exception:
        logging.exception("Failed to create database indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, int):
        return (dt // 1000) * 1000
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp()) * 1000
    return None


def epoch_ms_to_datetime(ms: int) -> datetime:
    ms_truncated = (ms // 1000) * 1000
    return datetime.fromtimestamp(ms_truncated / 1000.0, tz=timezone.utc)


def get_attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    pin_ist = to_ist(punch_in)
    end_h, end_m = map(int, shift_end.split(":"))
    shift_end_t = time(end_h, end_m)
    if shift_end <= shift_start and pin_ist.time() < shift_end_t:
        return (pin_ist.date() - timedelta(days=1)).isoformat()
    return pin_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    pin_ist = to_ist(punch_in).replace(microsecond=0)
    sh, sm = map(int, shift_start.split(":"))
    base_d = datetime.strptime(attendance_date, "%Y-%m-%d").date()
    start_dt = datetime(base_d.year, base_d.month, base_d.day, sh, sm, 0, tzinfo=IST)
    diff_sec = (pin_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    pin = punch_in.replace(microsecond=0)
    pout = punch_out.replace(microsecond=0)
    secs = int((pout - pin).total_seconds())
    hours = (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    pout_ist = to_ist(punch_out).replace(microsecond=0)
    eh, em = map(int, shift_end.split(":"))
    base_d = datetime.strptime(date_str, "%Y-%m-%d").date()
    if shift_end <= shift_start:
        base_d += timedelta(days=1)
    end_dt = datetime(base_d.year, base_d.month, base_d.day, eh, em, 0, tzinfo=IST)
    diff_sec = (pout_ist - end_dt).total_seconds()
    minutes = int(diff_sec // 60) if diff_sec > 0 else 0
    return minutes if minutes >= 30 else 0


def compute_working_days(month_str: str, joined_on_str: Optional[str]) -> int:
    y = int(month_str[:4])
    m = int(month_str[5:7])
    start_of_month = date(y, m, 1)
    if m == 12:
        next_month_date = date(y + 1, 1, 1)
    else:
        next_month_date = date(y, m + 1, 1)
    end_of_month = next_month_date - timedelta(days=1)

    if joined_on_str:
        try:
            joined_on_date = datetime.strptime(joined_on_str, "%Y-%m-%d").date()
        except ValueError:
            joined_on_date = start_of_month
    else:
        joined_on_date = start_of_month

    effective_start = max(start_of_month, joined_on_date)
    if effective_start > end_of_month:
        return 0

    working_days = 0
    cur = effective_start
    while cur <= end_of_month:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    y = int(month[:4])
    m = int(month[5:7])
    if m == 12:
        next_month_str = f"{y + 1:04d}-01-01"
    else:
        next_month_str = f"{y:04d}-{m + 1:02d}-01"
    start_month_str = f"{month}-01"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_month_str, "$lt": next_month_str},
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
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]
    return pipeline


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    y = int(month[:4])
    m = int(month[5:7])
    if m == 12:
        next_month_date = date(y + 1, 1, 1)
        next_month_str = f"{y + 1:04d}-01-01"
    else:
        next_month_date = date(y, m + 1, 1)
        next_month_str = f"{y:04d}-{m + 1:02d}-01"
    end_of_month_str = (next_month_date - timedelta(days=1)).isoformat()
    start_month_str = f"{month}-01"

    match_stage = {"joined_on": {"$lte": end_of_month_str}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": start_month_str, "$lt": next_month_str}
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "emp_stats": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": {
                            "present_days": 0.0,
                            "work_hours_sum": 0.0,
                            "work_hours_count": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                        "in": {
                            "present_days": {
                                "$add": [
                                    "$$value.present_days",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {
                                                        "$lte": [
                                                            {
                                                                "$isoDayOfWeek": {
                                                                    "$dateFromString": {"dateString": "$$this.date"}
                                                                }
                                                            },
                                                            5,
                                                        ]
                                                    },
                                                ]
                                            },
                                            {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                            0,
                                        ]
                                    },
                                ]
                            },
                            "work_hours_sum": {
                                "$add": [
                                    "$$value.work_hours_sum",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            {"$ifNull": ["$$this.work_hours", 0.0]},
                                            0.0,
                                        ]
                                    },
                                ]
                            },
                            "work_hours_count": {
                                "$add": [
                                    "$$value.work_hours_count",
                                    {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                    {"$ne": ["$$this.work_hours", None]},
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    },
                                ]
                            },
                            "late_count": {
                                "$add": [
                                    "$$value.late_count",
                                    {"$cond": [{"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0]},
                                ]
                            },
                            "total_late_minutes": {
                                "$add": [
                                    "$$value.total_late_minutes",
                                    {"$ifNull": ["$$this.late_minutes", 0]},
                                ]
                            },
                            "leave_count": {
                                "$add": [
                                    "$$value.leave_count",
                                    {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]},
                                ]
                            },
                            "on_duty_count": {
                                "$add": [
                                    "$$value.on_duty_count",
                                    {"$cond": [{"$eq": ["$$this.status", "ON_DUTY"]}, 1, 0]},
                                ]
                            },
                        },
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_stats.present_days"},
                "work_hours_sum": {"$sum": "$emp_stats.work_hours_sum"},
                "work_hours_count": {"$sum": "$emp_stats.work_hours_count"},
                "late_count": {"$sum": "$emp_stats.late_count"},
                "total_late_minutes": {"$sum": "$emp_stats.total_late_minutes"},
                "leave_count": {"$sum": "$emp_stats.leave_count"},
                "on_duty_count": {"$sum": "$emp_stats.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]
    return pipeline


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> list:
    year, m = map(int, month.split("-"))
    start_date = f"{year:04d}-{m:02d}-01"
    if m == 12:
        end_date = f"{year + 1:04d}-01-01"
    else:
        end_date = f"{year:04d}-{m + 1:02d}-01"

    pipeline = [
        {"$match": {"date": {"$gte": start_date, "$lt": end_date}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    department: str, date_from: str, date_to: str
) -> list:
    d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
    d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    num_days = (d_to - d_from).days + 1

    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "emps": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            }
        },
        {
            "$project": {
                "emps": 1,
                "day": {
                    "$map": {
                        "input": {"$range": [0, num_days]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {"$dateFromString": {"dateString": date_from}},
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$day"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "day",
                "foreignField": "date",
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$day"]},
                        }
                    }
                },
                "dept_logs": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {"$in": ["$$l.emp_code", "$emps.emp_code"]},
                    }
                },
                "is_working_day": {
                    "$lte": [
                        {
                            "$isoDayOfWeek": {
                                "$dateFromString": {"dateString": "$day"}
                            }
                        },
                        5,
                    ]
                },
            }
        },
        {
            "$addFields": {
                "counts": {
                    "$reduce": {
                        "input": "$dept_logs",
                        "initialValue": {
                            "present_count": 0.0,
                            "late_count": 0,
                        },
                        "in": {
                            "present_count": {
                                "$add": [
                                    "$$value.present_count",
                                    {
                                        "$cond": [
                                            {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]},
                                            0.0,
                                        ]
                                    },
                                ]
                            },
                            "late_count": {
                                "$add": [
                                    "$$value.late_count",
                                    {
                                        "$cond": [
                                            {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]},
                                            1,
                                            0,
                                        ]
                                    },
                                ]
                            },
                        },
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$counts.present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": "$day",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": "$counts.present_count",
                "late_count": "$counts.late_count",
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]
    return pipeline


def format_trend_item(doc: dict) -> dict:
    present_raw = doc.get("present_count", 0) or 0
    present_dec = Decimal(str(present_raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    present_count = int(present_dec) if present_dec % 1 == 0 else float(present_dec)

    att_rate_raw = doc.get("attendance_rate")
    if att_rate_raw is not None:
        att_rate = float(
            Decimal(str(att_rate_raw)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )
    else:
        att_rate = None

    mavg_raw = doc.get("moving_avg_7d")
    if mavg_raw is not None:
        mavg = float(
            Decimal(str(mavg_raw)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )
    else:
        mavg = None

    return {
        "date": doc["date"],
        "is_working_day": bool(doc["is_working_day"]),
        "headcount": int(doc["headcount"]),
        "present_count": present_count,
        "late_count": int(doc["late_count"]),
        "attendance_rate": att_rate,
        "moving_avg_7d": mavg,
    }


def format_employee(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    created_at = d.get("created_at")
    if isinstance(created_at, datetime):
        d["created_at"] = datetime_to_epoch_ms(created_at)
    return d


def format_attendance_record(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)

    d["half_day"] = bool(d.get("half_day", False) or False)
    d["history"] = d.get("history") or []
    d["late_minutes"] = d.get("late_minutes", 0) or 0
    d["overtime_minutes"] = d.get("overtime_minutes", 0) or 0
    d.setdefault("work_hours", None)

    d["punch_in"] = datetime_to_epoch_ms(d.get("punch_in"))
    d["punch_out"] = datetime_to_epoch_ms(d.get("punch_out"))

    formatted_history = []
    for h in d.get("history", []):
        h_copy = dict(h)
        at_val = h_copy.get("at")
        if at_val is not None:
            h_copy["at"] = datetime_to_epoch_ms(at_val)
        changes = h_copy.get("changes")
        if isinstance(changes, dict):
            new_changes = {}
            for k, v in changes.items():
                if isinstance(v, dict):
                    v_copy = dict(v)
                    if k in ("punch_in", "punch_out"):
                        if "from" in v_copy:
                            v_copy["from"] = datetime_to_epoch_ms(v_copy["from"])
                        if "to" in v_copy:
                            v_copy["to"] = datetime_to_epoch_ms(v_copy["to"])
                    new_changes[k] = v_copy
                else:
                    new_changes[k] = v
            h_copy["changes"] = new_changes
        formatted_history.append(h_copy)
    d["history"] = formatted_history
    return d


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            datetime.strptime(self.joined_on, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    model_config = {"extra": "ignore"}


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = epoch_ms_to_datetime(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    d = get_attendance_date(ts, shift_start, shift_end)

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, shift_start, d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = epoch_ms_to_datetime(body.punched_at)
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("date", -1)],
    )
    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    diff_sec = (ts - record["punch_in"]).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(record["punch_in"], ts)
    shift_end = emp.get("shift_end", "18:30")
    shift_start = emp.get("shift_start", "09:30")
    overtime_minutes = compute_overtime(ts, shift_end, record["date"], shift_start)
    half_day = work_hours < 4.50

    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_in": record["punch_in"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Already punched out")

    return format_attendance_record(updated_doc)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=422, detail="Invalid date format")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    stored_status = record.get("status")
    stored_punch_in = record.get("punch_in")
    stored_punch_out = record.get("punch_out")
    stored_work_hours = record.get("work_hours")
    stored_late_minutes = record.get("late_minutes", 0) or 0
    stored_overtime_minutes = record.get("overtime_minutes", 0) or 0
    stored_half_day = bool(record.get("half_day", False) or False)

    new_status = body.status if body.status is not None else stored_status
    new_punch_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else stored_punch_in
    new_punch_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else stored_punch_out

    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times with ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        final_punch_in = new_punch_in
        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        if body.punch_in is not None:
            calc_date = get_attendance_date(final_punch_in, shift_start, shift_end)
            if calc_date != date:
                raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        final_punch_out = new_punch_out
        if final_punch_out is not None:
            diff_sec = (final_punch_out - final_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, shift_end, date, shift_start)
            final_half_day = final_work_hours < 4.50
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

        final_late_minutes = compute_late_minutes(final_punch_in, shift_start, date)

    if (
        new_status == stored_status
        and final_punch_in == stored_punch_in
        and final_punch_out == stored_punch_out
    ):
        raise HTTPException(status_code=422, detail="Request does not change any fields")

    changes = {}
    if new_status != stored_status:
        changes["status"] = {"from": stored_status, "to": new_status}
    if final_punch_in != stored_punch_in:
        changes["punch_in"] = {"from": stored_punch_in, "to": final_punch_in}
    if final_punch_out != stored_punch_out:
        changes["punch_out"] = {"from": stored_punch_out, "to": final_punch_out}
    if final_work_hours != stored_work_hours:
        changes["work_hours"] = {"from": stored_work_hours, "to": final_work_hours}
    if final_late_minutes != stored_late_minutes:
        changes["late_minutes"] = {"from": stored_late_minutes, "to": final_late_minutes}
    if final_overtime_minutes != stored_overtime_minutes:
        changes["overtime_minutes"] = {"from": stored_overtime_minutes, "to": final_overtime_minutes}
    if final_half_day != stored_half_day:
        changes["half_day"] = {"from": stored_half_day, "to": final_half_day}

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    filter_query = {
        "emp_code": emp_code,
        "date": date,
        "status": stored_status,
        "punch_in": stored_punch_in,
        "punch_out": stored_punch_out,
    }
    if "history" in record and isinstance(record["history"], list):
        filter_query["history"] = {"$size": len(record["history"])}
    else:
        filter_query["history"] = {"$exists": False}

    updated_doc = db.attendance_logs.find_one_and_update(
        filter_query,
        {
            "$set": {
                "status": new_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record was modified concurrently")

    return format_attendance_record(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from format")
    if date_to is not None:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to format")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly", status_code=200)
def get_employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = compute_working_days(month, emp.get("joined_on"))

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_results = list(db.attendance_logs.aggregate(pipeline))

    if agg_results:
        row = agg_results[0]
        raw_present_days = row.get("present_days", 0) or 0
        leave_days = int(row.get("leave_days", 0) or 0)
        late_count = int(row.get("late_count", 0) or 0)
        total_late_minutes = int(row.get("total_late_minutes", 0) or 0)
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0) or 0)
    else:
        raw_present_days = 0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    present_days_dec = Decimal(str(raw_present_days)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    present_days = int(present_days_dec) if present_days_dec % 1 == 0 else float(present_days_dec)

    if working_days == 0:
        attendance_pct = None
    else:
        pct_dec = (
            Decimal(str(raw_present_days)) / Decimal(working_days) * Decimal(100)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        attendance_pct = int(pct_dec) if pct_dec % 1 == 0 else float(pct_dec)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", status_code=200)
def get_department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format")

    pipeline = build_department_summary_pipeline(month, department)
    agg_results = list(db.employees.aggregate(pipeline))

    items = []
    for doc in agg_results:
        dept_name = doc["_id"]
        headcount = int(doc.get("headcount", 0))
        raw_present_days = doc.get("present_days", 0) or 0
        wh_sum = doc.get("work_hours_sum", 0) or 0
        wh_count = int(doc.get("work_hours_count", 0) or 0)
        late_count = int(doc.get("late_count", 0) or 0)
        total_late_minutes = int(doc.get("total_late_minutes", 0) or 0)
        leave_count = int(doc.get("leave_count", 0) or 0)
        on_duty_count = int(doc.get("on_duty_count", 0) or 0)

        present_days_dec = Decimal(str(raw_present_days)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        present_days = (
            int(present_days_dec) if present_days_dec % 1 == 0 else float(present_days_dec)
        )

        if wh_count == 0:
            avg_work_hours = None
        else:
            avg_dec = (Decimal(str(wh_sum)) / Decimal(wh_count)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            avg_work_hours = float(avg_dec)

        items.append(
            {
                "department": dept_name,
                "headcount": headcount,
                "present_days": present_days,
                "avg_work_hours": avg_work_hours,
                "late_count": late_count,
                "total_late_minutes": total_late_minutes,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", status_code=200)
def get_late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise HTTPException(status_code=422, detail="Invalid month format")

    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = [
        {
            "rank": int(doc["rank"]),
            "emp_code": str(doc["emp_code"]),
            "name": str(doc["name"]),
            "department": str(doc["department"]),
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"]),
        }
        for doc in cursor
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", status_code=200)
def get_department_trend(
    department: str,
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    date_regex = r"^\d{4}-\d{2}-\d{2}$"
    if not re.match(date_regex, date_from) or not re.match(date_regex, date_to):
        raise HTTPException(status_code=422, detail="Invalid date format")

    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        d_to = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid calendar date")

    if d_to < d_from:
        raise HTTPException(
            status_code=422, detail="'to' date must be greater than or equal to 'from' date"
        )

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, date_from, date_to)
    cursor = db.employees.aggregate(pipeline)
    items = [format_trend_item(doc) for doc in cursor]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
