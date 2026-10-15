"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from re import match
from pymongo import ReturnDocument
from decimal import ROUND_HALF_UP
from decimal import Decimal
from pydantic import StrictInt
from pymongo.errors import DuplicateKeyError
from datetime import date , time
from pydantic import model_validator
from contextlib import asynccontextmanager
from pydantic import Field
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from enum import Enum
import calendar

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


#-------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index([("emp_code", 1)], unique=True)

    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
  
    yield
    client.close()
    print("MongoDB connection closed")


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def ensure_utc(dt: datetime) -> datetime:
    "Ensure the datetiem recevied from db is UTC-aware and truncated to whole seconds."
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0)

def compute_late_minutes(punch_in: datetime, shift_start: str,date_str: str) -> int:
    """Compute the late minutes."""
    h, m = map(int, shift_start.split(":"))

    shift_start_IST = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    punch_in_ist = ensure_utc(punch_in).astimezone(IST)

    diff_seconds = (punch_in_ist-shift_start_IST).total_seconds()

    if diff_seconds <= 600:
        return 0
    else:
        return int(diff_seconds // 60)

def round_half_up(val: float | Decimal, decimals: int = 2) -> float:
    """Rounds a number to `decimals` places using standard half-up rounding (R4, R8)."""
    d = Decimal(str(val))
    target = Decimal("1" if decimals == 0 else "0." + "0" * (decimals - 1) + "1")
    return float(d.quantize(target, rounding=ROUND_HALF_UP))

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """(punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up. (R4)"""
    p_in = ensure_utc(punch_in)
    p_out = ensure_utc(punch_out)
    diff_seconds = Decimal(int((p_out - p_in).total_seconds()))
    hours = diff_seconds / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """
    Whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0. Handles overnight shifts. (R3)
    """
    end_h, end_m = map(int, shift_end.split(":"))
    end_dt = datetime.fromisoformat(date_str).replace(
        hour=end_h, minute=end_m, second=0, microsecond=0, tzinfo=IST
    )
    # R1: For overnight shifts (shift_end <= shift_start), shift_end falls on the next calendar day
    if shift_end <= shift_start:
        end_dt += timedelta(days=1)

    p_out_ist = ensure_utc(punch_out).astimezone(IST)
    diff_seconds = (p_out_ist - end_dt).total_seconds()
    overtime_mins = int(diff_seconds // 60)

    return overtime_mins if overtime_mins >= 30 else 0

def compute_half_day(work_hours: float) -> bool:
    """R5: half_day is true when rounded work_hours < 4.50."""
    return work_hours < 4.50

def convert_millisec(data: datetime) -> int:
    """Convert datetime to epoch milliseconds. R1"""
    if data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)

    data = data.astimezone(timezone.utc)
    data = data.replace(microsecond=0)

    return int(data.timestamp() * 1000)

def format_attendance_doc(doc: dict) -> dict:
    """Formats attendance document for API response (converts datetimes to epoch ms)."""
    doc.pop("_id", None)
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    doc.setdefault("work_hours", None)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("late_minutes", 0)
    
    # Convert punch_in & punch_out to epoch millis
    for field in ("punch_in", "punch_out"):
        val = doc.get(field)
        if isinstance(val, datetime):
            doc[field] = convert_millisec(val)
            
    # Convert history entries
    if "history" in doc and isinstance(doc["history"], list):
        for h in doc["history"]:
            if isinstance(h.get("at"), datetime):
                h["at"] = convert_millisec(h["at"])
            if "changes" in h and isinstance(h["changes"], dict):
                for ch_key, ch_val in h["changes"].items():
                    if isinstance(ch_val, dict):
                        for k in ("from", "to"):
                            if isinstance(ch_val.get(k), datetime):
                                ch_val[k] = convert_millisec(ch_val[k])
    return doc

def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Returns the IST attendance date (YYYY-MM-DD) for a given punch_in instant.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time is
    strictly earlier than shift_end belongs to the previous calendar day.
    """
    ts_ist = ensure_utc(punch_in_utc).astimezone(IST)
    if shift_end <= shift_start:
        end_h, end_m = map(int, shift_end.split(":"))
        if ts_ist.time() < time(end_h, end_m):
            return (ts_ist - timedelta(days=1)).date().isoformat()
    return ts_ist.date().isoformat()

def compute_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Computes Mon-Fri working days in `month_str` (YYYY-MM),
    starting from max(month_start, joined_on).
    """
    year, month = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(month_start, joined_date)

    if start_date > month_end:
        return 0

    working_days = 0
    curr = start_date
    while curr <= month_end:
        if curr.weekday() < 5:
            working_days = working_days + 1
        curr = curr + timedelta(days=1)
    return working_days

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"

class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"

class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$", description="Unique employee code, EMP+4–6 digits")
    name: str = Field(..., min_length=1, max_length=100, description="Employee name, 1–100 characters")
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",description="Valid email address")
    department: str = Field(..., min_length=1, max_length=50, description="Department name, 1–100 characters")
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Shift start time in HH:MM format (24-hour)")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Shift end time in HH:MM format (24-hour)")
    joined_on: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",description="Date in YYYY-MM-DD format")

    @model_validator(mode="after")
    def validate_shift_and_date(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")

        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        None,
         ge=100000000000,  
        le=4102444800000,
        description="Epoch milliseconds (13 digits)"
    )
    status: PresenceStatus = PresenceStatus.PRESENT

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        None,
        ge=100000000000,  
        le=4102444800000,  
        description="Epoch milliseconds (13 digits)"
    )

class RegularizeIn(BaseModel):
    status: Optional[StatusEnum] = None
    punch_in: Optional[StrictInt] = Field(
        None,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds (13 digits)",
    )
    punch_out: Optional[StrictInt] = Field(
        None,
        ge=100000000000,
        le=4102444800000,
        description="Epoch milliseconds (13 digits)",
    )
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Checks the health of the API."""
    try:
        client.admin.command("ping")
        print("MongoDB connection successful")
        return {"status": "ok"}
    except PyMongoError as e:
        raise HTTPException(503, "MongoDB connection failed")

"""---> Create employee <--- """
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    # Validation of the employee fields done using Pydantic model
    # convert the body into dictionary
    doc = body.model_dump(mode="json")
    
    # add created at
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)

    try:
        #insert document in employees collection
        db.employees.insert_one(doc)

    except DuplicateKeyError as e:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        ) from e

    except PyMongoError as e:
        raise HTTPException(
            status_code=500,
            detail="Database operation failed"
        ) from e

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        ) from e
    # remove the _id
    doc.pop("_id", None)
    # Convert the created_at to epoch milliseconds
    doc["created_at"] = convert_millisec(doc["created_at"])

    return doc

"""---> List of employees <--- """
@app.get("/employees", status_code=200)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # create empty query
    q = {}
    if department:
        # add department name to query if present
        q["department"] = department
    
    # get total count of employees
    total = db.employees.count_documents(q)
    # calculate skip value
    skip = (page - 1) * page_size
    # Fetch, sort by emp_code asc, skip and limit
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = list(cursor)
    # Convert created_at BSON datetime to epoch milliseconds for each employee
    for item in items:
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = convert_millisec(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}

"""--->employee punch in<--- """
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # check employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
   
    if body.punched_at is not None:
        # if punch in time is given, convert it to datetime
        ts = datetime.fromtimestamp(body.punched_at//1000,tz=timezone.utc).replace(microsecond=0)
    else:
        # if punch in time is not given, use current time
        ts = datetime.now(timezone.utc).replace(microsecond=0)
    
    # convert to IST
    IST = timezone(timedelta(hours=5, minutes=30))
    ts_ist = ts.astimezone(IST)
   
    # get the shift start and end times
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # check if overnight shift
    is_overnight = shift_end <= shift_start

    if is_overnight:
        # get shift hour and minute
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_time = time(end_h, end_m)

        # check if punch in time is before shift end time
        if ts_ist.time() < shift_end_time:
            # if punch in time is before shift end time, then date is previous day
            shift_d = (ts_ist-timedelta(days=1)).date()
            d = shift_d.isoformat()
        else:
            # if punch in time is after shift end time, then date is current day
            d = ts_ist.date().isoformat()
    else:
        # if not overnight shift, then date is current day
        d = ts_ist.date().isoformat()
      

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
        # insert document in attendance_logs collection
        res = db.attendance_logs.insert_one(doc)

    except DuplicateKeyError as e:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        ) from e

    except PyMongoError as e:
        raise HTTPException(
            status_code=500,
            detail="Database operation failed"
        ) from e

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="Internal server error"
        ) from e


    doc.pop("_id", None)
    res = format_attendance_doc(doc)
    print("res: ", res)
    return res

"""---> List of attendance <--- """
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[StatusEnum] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # check if date_from is less than or equal to date_to
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be less than or equal to date_to")

    # create empty query
    q = {}
    # if emp_code is present, add it to query
    if emp_code:
        q["emp_code"] = emp_code
    # if date_from or date_to is present, add it to query
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    # if status is present, add it to query
    if status:
        q["status"] = status.value
    
    # get total count of attendance logs
    total = db.attendance_logs.count_documents(q)
    # calculate skip value
    skip = (page - 1) * page_size
    # sort by date desc, then emp_code asc
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    # create list of attendance logs
    items = []
    for doc in cursor:
        res = format_attendance_doc(doc)
        items.append(res)

    return {"items": items, "total": total, "page": page, "page_size": page_size}

"""---> employee punch out <--- """
@app.post("/attendance/punch-out",status_code=200)
def punch_out_attendance(punch_out: PunchOutIn):

    # check if employee exists
    emp = db.employees.find_one({"emp_code": punch_out.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    
    # if punch out time is given, convert it to datetime
    if punch_out.punched_at is not None:
        ts_out = datetime.fromtimestamp(punch_out.punched_at//1000,tz=timezone.utc).replace(microsecond=0)
    else:
        # if punch out time is not given, use current time
        ts_out = datetime.now(timezone.utc).replace(microsecond=0)
    
    # find the latest record for this employee, where punch_in should not be None and less than or equal to punch out time
    record = db.attendance_logs.find_one(
        {"emp_code": punch_out.emp_code,
         "punch_in": {"$ne": None, "$lte": ts_out}
         },
        sort=[("date", -1)]
    )

    # if no record found, check if there is any record with punch_in
    if not record:
        any_record = db.attendance_logs.find_one(
            {
                "emp_code": punch_out.emp_code,
                "punch_in": {"$ne": None}
            },
            sort=[("date", -1)]
        )
        # if any record found means we try to punch out before punch in
        if any_record:
            raise HTTPException(422, "punched_at must be strictly after punch_in")

        raise HTTPException(404, "No punch-in record found for this employee")
    
    # if punch out time is already present, then raise error
    if record["punch_out"] is not None:
        raise HTTPException(409, "already punched out for this date")

    # convert punch in time to datetime
    ts_in = ensure_utc(record["punch_in"])
    
    diff = (ts_out - ts_in).total_seconds()

    # if diff is less than 0, means punch out time is before punch in time
    if diff <= 0:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    
    # if diff is more than 24 hours, means punch out time is more than 24 hours after punch in time
    if diff > 86400:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(ts_in, ts_out)
    overtime_minutes = compute_overtime(ts_out, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hours)

    # update the record
    try:
        updated_doc = db.attendance_logs.find_one_and_update(
            {"_id": record["_id"], "punch_out": None},
            {
                "$set": {
                    "punch_out": ts_out,
                    "work_hours": work_hours,
                    "overtime_minutes": overtime_minutes,
                    "half_day": half_day
                }
            },
            return_document=ReturnDocument.AFTER,
        )
    
    except PyMongoError as e:
        raise HTTPException(500,"Database operation failed") from e

    if not updated_doc:
        raise HTTPException(409, "already punched out for this date")
    
    res = format_attendance_doc(updated_doc)
    print("res: ", res)
    return res
    
"""--->regularize attendance <--- """
@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    body: RegularizeIn,
    record_date: str = Path(
        ...,
        alias="date",
        pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
    ),
):
    # validate calendar date (e.g. reject 2026-02-31 with 422)
    try:
        date.fromisoformat(record_date)
    except ValueError as e:
        raise HTTPException(422, "Invalid calendar date")

    # check employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # check attendance record exists
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if not record:
        raise HTTPException(404, "attendance record not found for this date")

    # Extract current values
    old_status = record["status"]
    old_punch_in = ensure_utc(record["punch_in"]) if record.get("punch_in") is not None else None
    old_punch_out = ensure_utc(record["punch_out"]) if record.get("punch_out") is not None else None
    old_work_hours = record.get("work_hours", None)
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    # use given status or old status
    new_status = body.status.value if body.status is not None else old_status

    # check if status is absent or leave means no punch in and punch out allowed
    if new_status in (StatusEnum.ABSENT.value, StatusEnum.LEAVE.value):
        
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422,"Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False

    else:
        # if status is present/wfh/on_duty, then we need to check punch in and punch out

        # if body have punch in, then convert it to datetime
        # else use old punch in
        if body.punch_in is not None:
            new_punch_in = datetime.fromtimestamp(body.punch_in // 1000, tz=timezone.utc).replace(microsecond=0)
        else:
            new_punch_in = old_punch_in

        # if body have punch out, then convert it to datetime
        # else use old punch out
        if body.punch_out is not None:
            new_punch_out = datetime.fromtimestamp(body.punch_out // 1000, tz=timezone.utc).replace(microsecond=0)
        else:
            new_punch_out = old_punch_out

        # In case body bot give puch in and old puch in also None then raise error
        if new_punch_in is None:
            raise HTTPException(422,"Presence status requires punch_in")

        # get the attendance date from punch in time
        new_punch_in_computed_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        
        # if attendance date is not same as record date, then raise error
        if new_punch_in_computed_date != record_date:
            raise HTTPException(422,"punch_in must fall on the record's attendance date")

    
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], record_date)

        # If punch_out exists then check it should be after punch in and within 24 hours
        if new_punch_out is not None:
            diff_seconds = (new_punch_out - new_punch_in).total_seconds()
            if diff_seconds <= 0 or diff_seconds > 86400:
                raise HTTPException(422,"punch_out must be strictly after punch_in and within 24 hours")
            
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], record_date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    
    old_state = {
        "status": old_status,
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": old_work_hours,
        "late_minutes": old_late_minutes,
        "overtime_minutes": old_overtime_minutes,
        "half_day": old_half_day,
    }
    new_state = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    # use for loop to iterate over the keys and check old and new values should be different then add in changes object
    #in the format of {key : {from : value, to : value}} for that fieldname
    #this value can be updated value or old value, 
    changes = {}
    for field_name in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if old_state[field_name] != new_state[field_name]:
            changes[field_name] = {
                "from": old_state[field_name],
                "to": new_state[field_name],
            }

    
    if not changes:
        raise HTTPException(422,"No changes detected")

    # create history entry in the format of {at : value, by : value, reason : value, changes : value}
    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency control so concurrent edits don't lose history
    # Match the exact previous state of the record in the filter
    concurrency_filter = {
        "_id": record["_id"],
        "status": old_status,
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
    }
    if "history" in record and isinstance(record["history"], list):
        concurrency_filter["history"] = {"$size": len(record["history"])}
    else:
        concurrency_filter["history"] = {"$exists": False}

    try:
        updated_doc = db.attendance_logs.find_one_and_update(
            concurrency_filter,
            {
                "$set": new_state,
                "$push": {"history": history_entry},
            },
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError as e:
        raise HTTPException(500,"Database operation failed")

    if not updated_doc:
        raise HTTPException(409,"Concurrent modification detected; please retry")

    res =  format_attendance_doc(updated_doc)

    return res

"""---> Employee monthly summary """
@app.get("/analytics/employees/{emp_code}/monthly", status_code=200)
def employee_monthly_summary(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM"),
):
    # verify employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404,"employee not found")

    # compute working days for emp from joined date
    working_days = compute_working_days(emp["joined_on"], month)

    # Determine month date range for aggregation query
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    date_from = f"{month}-01"
    date_to = f"{month}-{last_day:02d}"

    # MongoDB Aggregation Pipeline for attendance logs
    pipeline = [
        {   # match the attendance logs for the given emp_code and month and return all the doc to the next stage
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {   # project the required fields and use ifnull to replace the null values with 0 / false and return the doc to the next stage
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}} 
                },
            }
        },
        {   # group the documents and compute the required fields
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 
                                    {"$gte": ["$day_of_week", 2]},  
                                    {"$lte": ["$day_of_week", 6]},  
                                ]
                            },
                            
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    try:
        agg_result = list(db.attendance_logs.aggregate(pipeline))
    except PyMongoError as e:
        raise HTTPException(500, "Database operation failed")

    if agg_result:
        res = agg_result[0]
        present_days = float(res.get("present_days", 0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    # Calculate attendance percentage with half-up rounding
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    res = {
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
    

    return res

""" --> late comers leaderboard"""
@app.get("/analytics/leaderboard/late", status_code=200)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM"),
    limit: int = Query(10, ge=1, le=50, description="Leaderboard limit"),
    department: Optional[str] = None,
):
    # create a list of all the month from the given month to current month
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    date_from = f"{month}-01"
    date_to = f"{month}-{last_day:02d}"

    pipeline = [
        # Match attendance logs within the month that have late_minutes > 0
        {
            "$match": {
                "date": {"$gte": date_from, "$lte": date_to},
                "late_minutes": {"$gt": 0},
            }
        },
        # Group by emp_code to aggregate total late minutes and late count
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Lookup employee details from the employees collection
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        # Filter by department if specified
        *(
            [{"$match": {"employee.department": department}}]
            if department
            else []
        ),
        # Project fields required for ranking and response
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        # Apply MongoDB window function for standard competition ranking
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        # Apply limit AFTER ranking
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        # Sort final rows: total_late_minutes descending, then emp_code ascending
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ]

    try:
        items = list(db.attendance_logs.aggregate(pipeline))
    except PyMongoError as e:
        raise HTTPException(500, "Database operation failed")

    return {
        "month": month,
        "items": items,
    }



    
   

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out --> done
#   PATCH /attendance/{emp_code}/{date} --> done
#   GET   /analytics/employees/{emp_code}/monthly --> done
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late  --> done
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
