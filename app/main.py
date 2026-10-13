"""

Employee Attendance & Analytics API

Run: uvicorn app.main:app --port 8000

"""

import os

import re

import calendar

from datetime import date, datetime, timedelta, timezone

from decimal import Decimal, ROUND_HALF_UP

from typing import Optional

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pymongo import MongoClient, ASCENDING, DESCENDING

from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(

    os.getenv("MONGO_URI", "mongodb://localhost:27017"),

    serverSelectionTimeoutMS=5000,

)

db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}

ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

app = FastAPI(

    title="Employee Attendance & Analytics API",

    version="2.0.0",

)

# ---------------------------------------------------------------------

# Helpers

# ---------------------------------------------------------------------

def now_utc():

    return datetime.now(UTC).replace(microsecond=0)

def epoch_ms(dt):

    if dt is None:

        return None

    if dt.tzinfo is None:

        dt = dt.replace(tzinfo=UTC)

    return int(dt.timestamp() * 1000)

def from_epoch_ms(value):

    if value is None:

        return None

    if isinstance(value, bool) or not isinstance(value, int):

        raise HTTPException(422, "Timestamp must be an integer in epoch milliseconds")

    if not 100000000000 <= value <= 4102444800000:

        raise HTTPException(422, "Timestamp is outside the allowed range")

    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)

def serialize_value(value):

    if isinstance(value, datetime):

        return epoch_ms(value)

    if isinstance(value, dict):

        return {k: serialize_value(v) for k, v in value.items() if k != "_id"}

    if isinstance(value, list):

        return [serialize_value(v) for v in value]

    return value

def serialize_employee(doc):

    if not doc:

        return None

    doc = dict(doc)

    doc.pop("_id", None)

    if "created_at" in doc:

        doc["created_at"] = epoch_ms(doc["created_at"])

    return doc

def serialize_attendance(doc):

    if not doc:

        return None

    result = dict(doc)

    result.pop("_id", None)

    result.setdefault("punch_in", None)

    result.setdefault("punch_out", None)

    result.setdefault("work_hours", None)

    result.setdefault("late_minutes", 0)

    result.setdefault("overtime_minutes", 0)

    result.setdefault("half_day", False)

    result.setdefault("history", [])

    result["punch_in"] = epoch_ms(result["punch_in"])

    result["punch_out"] = epoch_ms(result["punch_out"])

    for entry in result["history"]:

        entry["at"] = epoch_ms(entry.get("at"))

        for field, change in entry.get("changes", {}).items():

            if field in {"punch_in", "punch_out"}:

                change["from"] = epoch_ms(change.get("from"))

                change["to"] = epoch_ms(change.get("to"))

    return result

def round_half_up(value, places=2):

    quantum = Decimal("1").scaleb(-places)

    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))

def valid_date(value, field="date"):

    try:

        parsed = date.fromisoformat(value)

        if parsed.isoformat() != value:

            raise ValueError

        return parsed

    except (ValueError, TypeError):

        raise HTTPException(422, f"{field} must be YYYY-MM-DD")

def valid_month(value):

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):

        raise HTTPException(422, "month must be YYYY-MM")

    year, month = map(int, value.split("-"))

    start = date(year, month, 1)

    end = date(year, month, calendar.monthrange(year, month)[1])

    return start, end

def validate_shift(value):

    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value or ""):

        raise HTTPException(422, "Shift time must use HH:MM format")

    return value

def attendance_date(punch_in, employee):

    local = punch_in.astimezone(IST)

    shift_start = validate_shift(employee["shift_start"])

    shift_end = validate_shift(employee["shift_end"])

    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:

        local -= timedelta(days=1)

    return local.date().isoformat()

def compute_late_minutes(punch_in, shift_start):

    local = punch_in.astimezone(IST)

    h, m = map(int, validate_shift(shift_start).split(":"))

    start = local.replace(hour=h, minute=m, second=0, microsecond=0)

    seconds = int((local - start).total_seconds())

    return seconds // 60 if seconds > 600 else 0

def shift_end_datetime(attendance_day, employee):

    shift_end = validate_shift(employee["shift_end"])

    shift_start = validate_shift(employee["shift_start"])

    end_date = date.fromisoformat(attendance_day)

    if shift_end <= shift_start:

        end_date += timedelta(days=1)

    h, m = map(int, shift_end.split(":"))

    return datetime(

        end_date.year, end_date.month, end_date.day,

        h, m, tzinfo=IST,

    ).astimezone(UTC)

def compute_overtime(punch_out, shift_end, attendance_day, employee):

    end = shift_end_datetime(attendance_day, employee)

    minutes = int((punch_out - end).total_seconds() // 60)

    return minutes if minutes >= 30 else 0

def compute_work_hours(punch_in, punch_out):

    seconds = (punch_out - punch_in).total_seconds()

    return round_half_up(seconds / 3600, 2)

def calculate_derived(record, employee):

    punch_in = record.get("punch_in")

    punch_out = record.get("punch_out")

    if record["status"] not in PRESENCE_STATUSES:

        record["punch_in"] = None

        record["punch_out"] = None

        record["work_hours"] = None

        record["late_minutes"] = 0

        record["overtime_minutes"] = 0

        record["half_day"] = False

        return record

    if punch_in is None:

        raise HTTPException(422, "Presence status requires punch_in")

    record["late_minutes"] = compute_late_minutes(

        punch_in, employee["shift_start"]

    )

    if punch_out is None:

        record["work_hours"] = None

        record["overtime_minutes"] = 0

        record["half_day"] = False

    else:

        seconds = (punch_out - punch_in).total_seconds()

        if seconds <= 0 or seconds > 86400:

            raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")

        record["work_hours"] = compute_work_hours(punch_in, punch_out)

        record["overtime_minutes"] = compute_overtime(

            punch_out, employee["shift_end"], record["date"], employee

        )

        record["half_day"] = record["work_hours"] < 4.50

    return record

def working_days(start, end):

    count = 0

    current = start

    while current <= end:

        if current.weekday() < 5:

            count += 1

        current += timedelta(days=1)

    return count

def month_filter(month):

    start, end = valid_month(month)

    return start.isoformat(), end.isoformat()

def ensure_indexes():

    db.employees.create_index(

        [("emp_code", ASCENDING)],

        unique=True,

        name="uq_employee_code",

    )

    db.employees.create_index(

        [("department", ASCENDING), ("joined_on", ASCENDING)],

        name="ix_employee_department_joined",

    )

    db.attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", ASCENDING)],

        unique=True,

        name="uq_attendance_employee_date",

    )

    db.attendance_logs.create_index(

        [("date", DESCENDING), ("emp_code", ASCENDING)],

        name="ix_attendance_date_employee",

    )

    db.attendance_logs.create_index(

        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],

        name="ix_attendance_employee_date_status",

    )

@app.on_event("startup")

def startup():

    ensure_indexes()

# ---------------------------------------------------------------------

# Request models

# ---------------------------------------------------------------------

class EmployeeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(

        max_length=120,

        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",

    )

    department: str = Field(min_length=1, max_length=50)

    shift_start: str = "09:30"

    shift_end: str = "18:30"

    joined_on: str

    @field_validator("shift_start", "shift_end")

    @classmethod

    def check_shift(cls, value):

        return validate_shift(value)

    @field_validator("joined_on")

    @classmethod

    def check_joined_on(cls, value):

        valid_date(value, "joined_on")

        return value

    @field_validator("shift_end")

    @classmethod

    def end_not_equal_start(cls, value, info):

        start = info.data.get("shift_start")

        if start is not None and start == value:

            raise ValueError("shift_start and shift_end must differ")

        return value

class PunchInIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[int] = None

    status: str = "PRESENT"

    @field_validator("punched_at")

    @classmethod

    def check_timestamp(cls, value):

        if value is not None:

            from_epoch_ms(value)

        return value

    @field_validator("status")

    @classmethod

    def check_status(cls, value):

        if value not in PRESENCE_STATUSES:

            raise ValueError("Invalid punch-in status")

        return value

class PunchOutIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    emp_code: str

    punched_at: Optional[int] = None

    @field_validator("punched_at")

    @classmethod

    def check_timestamp(cls, value):

        if value is not None:

            from_epoch_ms(value)

        return value

class RegularizeIn(BaseModel):

    model_config = ConfigDict(extra="ignore")

    status: Optional[str] = None

    punch_in: Optional[int] = None

    punch_out: Optional[int] = None

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")

    @classmethod

    def check_status(cls, value):

        if value is not None and value not in ALL_STATUSES:

            raise ValueError("Invalid attendance status")

        return value

    @field_validator("punch_in", "punch_out")

    @classmethod

    def check_timestamp(cls, value):

        if value is not None:

            from_epoch_ms(value)

        return value

# ---------------------------------------------------------------------

# Health and employees

# ---------------------------------------------------------------------

@app.get("/health")

def health():

    try:

        db.command("ping")

        return {"status": "ok"}

    except Exception:

        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["created_at"] = now_utc()

    try:

        db.employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(doc)

@app.get("/employees")

def list_employees(

    department: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    query = {}

    if department is not None:

        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(

        db.employees.find(query, {"_id": 0})

        .sort("emp_code", ASCENDING)

        .skip((page - 1) * page_size)

        .limit(page_size)

    )

    return {

        "items": [serialize_employee(x) for x in items],

        "total": total,

        "page": page,

        "page_size": page_size,

    }

# ---------------------------------------------------------------------

# Punch-in / punch-out

# ---------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:

        raise HTTPException(404, "Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()

    day = attendance_date(ts, employee)

    record = {

        "emp_code": body.emp_code,

        "date": day,

        "status": body.status,

        "punch_in": ts,

        "punch_out": None,

        "work_hours": None,

        "late_minutes": compute_late_minutes(ts, employee["shift_start"]),

        "overtime_minutes": 0,

        "half_day": False,

        "history": [],

    }

    try:

        db.attendance_logs.insert_one(record)

    except DuplicateKeyError:

        raise HTTPException(409, "Already punched in for this attendance date")

    return serialize_attendance(record)

@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:

        raise HTTPException(404, "Employee not found")

    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()

    record = db.attendance_logs.find_one(

        {

            "emp_code": body.emp_code,

            "punch_in": {"$ne": None, "$lte": ts},

        },

        sort=[("punch_in", DESCENDING)],

    )

    if not record:

        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:

        raise HTTPException(409, "Already punched out")
    punch_in_time = record["punch_in"]
    # PyMongo returns naive UTC datetimes by default.
    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(tzinfo=UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    seconds = (ts - punch_in_time).total_seconds()
    if seconds <= 0 or seconds > 86400:

        raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")

    updated = db.attendance_logs.find_one_and_update(

        {

            "_id": record["_id"],

            "punch_out": None,

        },

        {

            "$set": {

                "punch_out": ts,

                "work_hours": compute_work_hours(punch_in_time, ts),

                "overtime_minutes": compute_overtime(

                    ts, employee["shift_end"], record["date"], employee

                ),

                "half_day": compute_work_hours(punch_in_time, ts) < 4.50,

            }

        },

        return_document=True,

    )

    if not updated:

        raise HTTPException(409, "Record was already punched out")

    return serialize_attendance(updated)

# ---------------------------------------------------------------------

# Attendance listing

# ---------------------------------------------------------------------

@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    query = {}

    if emp_code:

        query["emp_code"] = emp_code

    if date_from:

        valid_date(date_from, "date_from")

    if date_to:

        valid_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:

        raise HTTPException(422, "date_from must not be after date_to")

    if date_from or date_to:

        query["date"] = {}

        if date_from:

            query["date"]["$gte"] = date_from

        if date_to:

            query["date"]["$lte"] = date_to

    if status:

        if status not in ALL_STATUSES:

            raise HTTPException(422, "Invalid status")

        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    docs = list(

        db.attendance_logs.find(query)

        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])

        .skip((page - 1) * page_size)

        .limit(page_size)

    )

    return {

        "items": [serialize_attendance(x) for x in docs],

        "total": total,

        "page": page,

        "page_size": page_size,

    }

# ---------------------------------------------------------------------

# Manual correction / audit trail

# ---------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):

    valid_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:

        raise HTTPException(404, "Employee not found")

    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})

    if not old:

        raise HTTPException(404, "Attendance record not found")

    new = dict(old)

    new["history"] = list(old.get("history", []))

    if body.status is not None:

        new["status"] = body.status

    if body.status in {"ABSENT", "LEAVE"}:

        if body.punch_in is not None or body.punch_out is not None:

            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")

        new["punch_in"] = None

        new["punch_out"] = None

    else:

        if body.punch_in is not None:

            new["punch_in"] = from_epoch_ms(body.punch_in)

        if body.punch_out is not None:

            new["punch_out"] = from_epoch_ms(body.punch_out)

    if new["status"] in PRESENCE_STATUSES and new.get("punch_in") is None:

        raise HTTPException(422, "Presence status requires punch_in")

    if new.get("punch_in") is not None:

        expected_date = attendance_date(new["punch_in"], employee)

        if expected_date != date:

            raise HTTPException(422, "punch_in must belong to the attendance date")

    calculate_derived(new, employee)

    fields = [

        "status", "punch_in", "punch_out",

        "work_hours", "late_minutes", "overtime_minutes", "half_day",

    ]

    changes = {}

    for field in fields:

        before = old.get(field)

        after = new.get(field)

        if before != after:

            changes[field] = {"from": before, "to": after}

    if not changes:

        raise HTTPException(422, "The correction does not change anything")

    entry = {

        "at": now_utc(),

        "by": body.regularized_by,

        "reason": body.reason,

        "changes": changes,

    }

    update = {

        field: new.get(field)

        for field in fields

    }

    result = db.attendance_logs.update_one(

        {

            "_id": old["_id"],

            "history": old.get("history", []),

        },

        {

            "$set": update,

            "$push": {"history": entry},

        },

    )

    if result.modified_count != 1:

        raise HTTPException(409, "Record changed concurrently; retry correction")

    saved = db.attendance_logs.find_one({"_id": old["_id"]})

    return serialize_attendance(saved)

# ---------------------------------------------------------------------

# Analytics

# ---------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str):

    start, end = valid_month(month)

    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:

        raise HTTPException(404, "Employee not found")

    joined = valid_date(employee["joined_on"], "joined_on")

    effective_start = max(start, joined)

    days = working_days(effective_start, end) if effective_start <= end else 0

    pipeline = [

        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},

        {"$group": {

            "_id": None,

            "present_days": {"$sum": {

                "$cond": [

                    {"$and": [

                        {"$in": ["$status", list(PRESENCE_STATUSES)]},

                        {"$eq": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},

                    ]},

                    0,

                    0,

                ]

            }},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},

            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},

            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},

        }},

    ]

    docs = list(db.attendance_logs.aggregate(pipeline))

    result = docs[0] if docs else {}

    # Calculate weekday presence accurately from stored attendance records.

    logs = list(db.attendance_logs.find({

        "emp_code": emp_code,

        "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},

    }, {"date": 1, "status": 1, "half_day": 1}))

    present = 0.0

    for log in logs:

        day = date.fromisoformat(log["date"])

        if day.weekday() < 5 and log.get("status") in PRESENCE_STATUSES:

            present += 0.5 if log.get("half_day", False) else 1.0

    return {

        "emp_code": emp_code,

        "month": month,

        "working_days": days,

        "present_days": round_half_up(present, 2),

        "leave_days": result.get("leave_days", 0),

        "late_count": result.get("late_count", 0),

        "total_late_minutes": result.get("total_late_minutes", 0),

        "total_overtime_minutes": result.get("total_overtime_minutes", 0),

        "attendance_pct": (

            round_half_up(present / days * 100, 4) if days else None

        ),

    }

@app.get("/analytics/departments/summary")

def department_summary(month: str, department: Optional[str] = None):

    start, end = valid_month(month)

    match = {

        "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}

    }

    pipeline = [

        {"$match": match},

        {"$lookup": {

            "from": "employees",

            "localField": "emp_code",

            "foreignField": "emp_code",

            "as": "employee",

        }},

        {"$unwind": "$employee"},

        *([{"$match": {"employee.department": department}}] if department else []),

        {"$group": {

            "_id": "$employee.department",

            "present_days": {"$sum": {

                "$cond": [

                    {"$and": [

                        {"$in": ["$status", list(PRESENCE_STATUSES)]},

                        {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]},

                        {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]},

                    ]},

                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},

                    0,

                ]

            }},

            "hours": {"$push": {

                "$cond": [

                    {"$and": [

                        {"$in": ["$status", list(PRESENCE_STATUSES)]},

                        {"$ne": ["$work_hours", None]},

                    ]},

                    "$work_hours",

                    None,

                ]

            }},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},

            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},

            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},

            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},

        }},

    ]

    aggregate = {d["_id"]: d for d in db.attendance_logs.aggregate(pipeline)}

    employees = list(db.employees.find(

        {"joined_on": {"$lte": end.isoformat()}},

        {"emp_code": 1, "department": 1, "joined_on": 1},

    ))

    departments = sorted({

        e["department"] for e in employees

        if department is None or e["department"] == department

    })

    items = []

    for dep in departments:

        headcount = sum(1 for e in employees if e["department"] == dep)

        if headcount == 0:

            continue

        data = aggregate.get(dep, {})

        hours = [

            x for x in data.get("hours", [])

            if x is not None

        ]

        # Calculate working-day present counts using the exact attendance dates.

        dep_logs = list(db.attendance_logs.find(

            {

                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},

                "status": {"$in": list(PRESENCE_STATUSES)},

            },

            {"emp_code": 1, "date": 1, "half_day": 1},

        ))

        codes = {

            e["emp_code"] for e in employees if e["department"] == dep

        }

        present = 0.0

        for log in dep_logs:

            if log["emp_code"] in codes and date.fromisoformat(log["date"]).weekday() < 5:

                present += 0.5 if log.get("half_day", False) else 1.0

        items.append({

            "department": dep,

            "headcount": headcount,

            "present_days": round_half_up(present, 2),

            "avg_work_hours": round_half_up(sum(hours) / len(hours), 2) if hours else None,

            "late_count": data.get("late_count", 0),

            "total_late_minutes": data.get("total_late_minutes", 0),

            "leave_count": data.get("leave_count", 0),

            "on_duty_count": data.get("on_duty_count", 0),

        })

    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")

def late_leaderboard(

    month: str,

    limit: int = Query(10, ge=1, le=50),

    department: Optional[str] = None,

):

    start, end = valid_month(month)

    pipeline = [

        {"$match": {

            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},

            "late_minutes": {"$gt": 0},

        }},

        {"$group": {

            "_id": "$emp_code",

            "total_late_minutes": {"$sum": "$late_minutes"},

            "late_count": {"$sum": 1},

        }},

        {"$lookup": {

            "from": "employees",

            "localField": "_id",

            "foreignField": "emp_code",

            "as": "employee",

        }},

        {"$unwind": "$employee"},

    ]

    if department:

        pipeline.append({"$match": {"employee.department": department}})

    pipeline += [

        {"$sort": {"total_late_minutes": -1, "_id": 1}},

        {"$setWindowFields": {

            "sortBy": {"total_late_minutes": -1},

            "output": {"rank": {"$rank": {}}},

        }},

        {"$match": {"rank": {"$lte": limit}}},

        {"$project": {

            "_id": 0,

            "rank": 1,

            "emp_code": "$_id",

            "name": "$employee.name",

            "department": "$employee.department",

            "total_late_minutes": 1,

            "late_count": 1,

        }},

    ]

    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):

    start = valid_date(from_date, "from")

    end = valid_date(to, "to")

    if end < start:

        raise HTTPException(422, "'to' must not be before 'from'")

    if (end - start).days + 1 > 92:

        raise HTTPException(422, "Date range must not exceed 92 days")

    if not db.employees.find_one({"department": department}):

        raise HTTPException(404, "Department not found")

    pipeline = [

        {"$match": {

            "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},

        }},

        {"$lookup": {

            "from": "employees",

            "localField": "emp_code",

            "foreignField": "emp_code",

            "as": "employee",

        }},

        {"$unwind": "$employee"},

        {"$match": {"employee.department": department}},

        {"$group": {

            "_id": "$date",

            "present_count": {"$sum": {

                "$cond": [

                    {"$in": ["$status", list(PRESENCE_STATUSES)]},

                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},

                    0,

                ]

            }},

            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},

        }},

        {"$project": {

            "_id": 0,

            "date": "$_id",

            "present_count": 1,

            "late_count": 1,

        }},

        {"$sort": {"date": 1}},

    ]

    # Fetch aggregated day totals; the output range is gap-filled below.

    # Use MongoDB aggregation for all recorded attendance values.

    totals = {row["date"]: row for row in db.attendance_logs.aggregate(pipeline)}

    employees = list(db.employees.find(

        {"department": department, "joined_on": {"$lte": end.isoformat()}},

        {"joined_on": 1},

    ))

    rows = []

    current = start

    rates = []

    while current <= end:

        day_str = current.isoformat()

        headcount = sum(

            1 for e in employees

            if e["joined_on"] <= day_str

        )

        record = totals.get(day_str, {})

        present = record.get("present_count", 0)

        late = record.get("late_count", 0)

        is_working = current.weekday() < 5

        rate = (

            round_half_up(present / headcount, 4)

            if is_working and headcount > 0

            else None

        )

        rates.append(rate)

        recent = [x for x in rates[-7:] if x is not None]

        moving = round_half_up(sum(recent) / len(recent), 4) if recent else None

        rows.append({

            "date": day_str,

            "is_working_day": is_working,

            "headcount": headcount,

            "present_count": present,

            "late_count": late,

            "attendance_rate": rate,

            "moving_avg_7d": moving,

        })

        current += timedelta(days=1)

    return {"department": department, "items": rows}

# ---------------------------------------------------------------------

# Explain endpoint

# ---------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")

def explain_endpoint(

    endpoint: str,

    emp_code: Optional[str] = None,

    month: Optional[str] = None,

    department: Optional[str] = None,

    limit: int = Query(10, ge=1, le=50),

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[str] = None,

    from_date: Optional[str] = Query(None, alias="from"),

    to: Optional[str] = None,

    page: int = Query(1, ge=1),

    page_size: int = Query(20, ge=1, le=100),

):

    if endpoint == "attendance_list":

        query = {}

        if emp_code:

            query["emp_code"] = emp_code

        if status:

            if status not in ALL_STATUSES:

                raise HTTPException(422, "Invalid status")

            query["status"] = status

        if date_from:

            valid_date(date_from, "date_from")

        if date_to:

            valid_date(date_to, "date_to")

        if date_from or date_to:

            query["date"] = {}

            if date_from:

                query["date"]["$gte"] = date_from

            if date_to:

                query["date"]["$lte"] = date_to

        explanation = db.command({

            "explain": {

                "find": "attendance_logs",

                "filter": query,

                "sort": {"date": -1, "emp_code": 1},

                "skip": (page - 1) * page_size,

                "limit": page_size,

            },

            "verbosity": "executionStats",

        })

        return {

            "endpoint": endpoint,

            "collection": "attendance_logs",

            "explain": explanation,

        }

    if endpoint == "employee_monthly":

        if not emp_code or not month:

            raise HTTPException(422, "emp_code and month are required")

        start, end = valid_month(month)

        pipeline = [

            {"$match": {

                "emp_code": emp_code,

                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},

            }},

            {"$sort": {"date": 1}},

        ]

        explanation = db.command({

            "explain": {

                "aggregate": "attendance_logs",

                "pipeline": pipeline,

                "cursor": {},

            },

            "verbosity": "executionStats",

        })

        return {

            "endpoint": endpoint,

            "collection": "attendance_logs",

            "explain": explanation,

        }

    if endpoint == "department_summary":

        if not month:

            raise HTTPException(422, "month is required")

        start, end = valid_month(month)

        pipeline = [

            {"$match": {

                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},

            }},

            {"$sort": {"date": 1}},

        ]

        explanation = db.command({

            "explain": {

                "aggregate": "attendance_logs",

                "pipeline": pipeline,

                "cursor": {},

            },

            "verbosity": "executionStats",

        })

        return {

            "endpoint": endpoint,

            "collection": "attendance_logs",

            "explain": explanation,

        }

    if endpoint == "late_leaderboard":

        if not month:

            raise HTTPException(422, "month is required")

        start, end = valid_month(month)

        pipeline = [

            {"$match": {

                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},

                "late_minutes": {"$gt": 0},

            }},

            {"$sort": {"date": 1, "emp_code": 1}},

        ]

        explanation = db.command({

            "explain": {

                "aggregate": "attendance_logs",

                "pipeline": pipeline,

                "cursor": {},

            },

            "verbosity": "executionStats",

        })

        return {

            "endpoint": endpoint,

            "collection": "attendance_logs",

            "explain": explanation,

        }

    if endpoint == "department_trend":

        if not department or not from_date or not to:

            raise HTTPException(422, "department, from and to are required")

        start = valid_date(from_date, "from")

        end = valid_date(to, "to")

        if end < start or (end - start).days + 1 > 92:

            raise HTTPException(422, "Invalid trend date range")

        pipeline = [

            {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},

            {"$sort": {"date": 1}},

        ]

        explanation = db.command({

            "explain": {

                "aggregate": "attendance_logs",

                "pipeline": pipeline,

                "cursor": {},

            },

            "verbosity": "executionStats",

        })

        return {

            "endpoint": endpoint,

            "collection": "attendance_logs",

            "explain": explanation,

        }

    raise HTTPException(422, "Unsupported endpoint")