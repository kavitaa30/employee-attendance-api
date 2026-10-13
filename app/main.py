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
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
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


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
