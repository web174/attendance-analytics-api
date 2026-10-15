import os

import math

import calendar

from decimal import Decimal, ROUND_HALF_UP

from datetime import datetime, date, timedelta, timezone

from typing import Optional, List, Dict, Any



from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query, Path, Response, status

from pydantic import BaseModel, Field

from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument

from pymongo.errors import DuplicateKeyError



load_dotenv()



MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")



client = MongoClient(MONGO_URI)

db = client[MONGO_DB]



IST = timezone(timedelta(hours=5, minutes=30))



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")



# --- Indexes Creation at Startup ---

@app.on_event("startup")

def create_indexes():

    db.employees.create_index([("emp_code", ASCENDING)], unique=True)

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)

    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])



# --- Business Rules Helpers (R1 - R5) ---

def round_half_up(val: float, decimals: int = 2) -> float:

    d = Decimal(str(val))

    target = Decimal('10') ** -decimals

    return float(d.quantize(target, rounding=ROUND_HALF_UP))



def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:

    if dt is None:

        return None

    if dt.tzinfo is None:

        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp() * 1000)



def truncate_to_seconds(dt: datetime) -> datetime:

    return dt.replace(microsecond=0)



def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:

    punch_ist = punch_in_utc.astimezone(IST)

    is_overnight = shift_end <= shift_start

    if is_overnight:

        end_h, end_m = map(int, shift_end.split(":"))

        shift_end_time = punch_ist.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        if punch_ist < shift_end_time:

            return (punch_ist.date() - timedelta(days=1)).isoformat()

    return punch_ist.date().isoformat()



def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date_str: str) -> int:

    att_date = date.fromisoformat(att_date_str)

    h, m = map(int, shift_start.split(":"))

    shift_start_ist = datetime(att_date.year, att_date.month, att_date.day, h, m, 0, tzinfo=IST)

    punch_ist = punch_in_utc.astimezone(IST)

    diff_sec = (punch_ist - shift_start_ist).total_seconds()

    if diff_sec > 600:  # strictly > 10 min grace period

        return int(diff_sec // 60)

    return 0



def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:

    att_date = date.fromisoformat(att_date_str)

    end_h, end_m = map(int, shift_end.split(":"))

    if shift_end <= shift_start:

        end_date = att_date + timedelta(days=1)

    else:

        end_date = att_date

    shift_end_ist = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST)

    punch_out_ist = punch_out_utc.astimezone(IST)

    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()

    minutes = int(diff_sec // 60)

    return minutes if minutes >= 30 else 0



def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:

    secs = (punch_out_utc - punch_in_utc).total_seconds()

    return round_half_up(secs / 3600.0, 2)



def serialize_log(doc: dict) -> dict:

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

        "history": [

            {

                "at": to_epoch_ms(h["at"]),

                "by": h["by"],

                "reason": h["reason"],

                "changes": {

                    k: {

                        "from": to_epoch_ms(v["from"]) if isinstance(v.get("from"), datetime) else v.get("from"),

                        "to": to_epoch_ms(v["to"]) if isinstance(v.get("to"), datetime) else v.get("to")

                    } for k, v in h.get("changes", {}).items()

                }

            } for h in doc.get("history", [])

        ]

    }



# --- Pydantic Schemas ---

class EmployeeIn(BaseModel):

    emp_code: str

    name: str

    email: str

    department: str

    shift_start: str = "09:30"

    shift_end: str = "18:30"

    joined_on: str



class PunchInIn(BaseModel):

    emp_code: str

    punched_at: Optional[int] = None

    status: str = "PRESENT"



class PunchOutIn(BaseModel):

    emp_code: str

    punched_at: Optional[int] = None



class RegularizeIn(BaseModel):

    status: Optional[str] = None

    punch_in: Optional[int] = None

    punch_out: Optional[int] = None

    reason: str

    regularized_by: str



# --- Endpoints ---

@app.get("/health")

def health():

    try:

        client.admin.command('ping')

        return {"status": "ok"}

    except Exception:

        raise HTTPException(status_code=503, detail="Database unreachable")



@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    if body.shift_start == body.shift_end:

        raise HTTPException(422, "shift_start must differ from shift_end")

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))

    doc = body.model_dump()

    doc["created_at"] = now_utc

    try:

        db.employees.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)

    doc["created_at"] = to_epoch_ms(doc["created_at"])

    return doc



@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):

    q = {}

    if department:

        q["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(q)

    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []

    for item in cursor:

        item["created_at"] = to_epoch_ms(item["created_at"])

        items.append(item)

    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(404, "Unknown employee")

    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:

        raise HTTPException(422, "Invalid status for punch-in")



    if body.punched_at:

        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)

    else:

        ts = datetime.now(timezone.utc)

    ts = truncate_to_seconds(ts)



    att_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])

    late_min = compute_late_minutes(ts, emp["shift_start"], att_date)



    doc = {

        "emp_code": body.emp_code,

        "date": att_date,

        "status": body.status,

        "punch_in": ts,

        "punch_out": None,

        "work_hours": None,

        "late_minutes": late_min,

        "overtime_minutes": 0,

        "half_day": False,

        "history": []

    }

    try:

        db.attendance_logs.insert_one(doc)

    except DuplicateKeyError:

        raise HTTPException(409, "Already punched in for this date")



    return serialize_log(doc)



@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:

        raise HTTPException(404, "Unknown employee")



    if body.punched_at:

        out_ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)

    else:

        out_ts = datetime.now(timezone.utc)

    out_ts = truncate_to_seconds(out_ts)



    record = db.attendance_logs.find_one(

        {"emp_code": body.emp_code, "punch_in": {"$lte": out_ts}},

        sort=[("punch_in", DESCENDING)]

    )

    if not record:

        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:

        raise HTTPException(409, "Record already punched out")



    in_ts = record["punch_in"]

    if in_ts.tzinfo is None:

        in_ts = in_ts.replace(tzinfo=timezone.utc)



    diff_sec = (out_ts - in_ts).total_seconds()

    if diff_sec <= 0 or diff_sec > 86400:

        raise HTTPException(422, "punch_out must be after punch_in and <= 24 hours")



    work_hrs = compute_work_hours(in_ts, out_ts)

    ot_min = compute_overtime_minutes(out_ts, emp["shift_start"], emp["shift_end"], record["date"])

    half_day = work_hrs < 4.50



    res = db.attendance_logs.find_one_and_update(

        {"_id": record["_id"], "punch_out": None},

        {"$set": {

            "punch_out": out_ts,

            "work_hours": work_hrs,

            "overtime_minutes": ot_min,

            "half_day": half_day

        }},

        return_document=ReturnDocument.AFTER

    )

    if not res:

        raise HTTPException(409, "Concurrent punch-out race")



    return serialize_log(res)



@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Optional[str] = None,

    date_to: Optional[str] = None,

    status: Optional[str] = None,

    page: int = 1,

    page_size: int = 20

):

    if date_from and date_to and date_from > date_to:

        raise HTTPException(422, "date_from cannot be greater than date_to")



    q = {}

    if emp_code:

        q["emp_code"] = emp_code

    if date_from or date_to:

        q["date"] = {}

        if date_from:

            q["date"]["$gte"] = date_from

        if date_to:

            q["date"]["$lte"] = date_to

    if status:

        q["status"] = status



    total = db.attendance_logs.count_documents(q)

    skip = (page - 1) * page_size

    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)



    return {

        "items": [serialize_log(d) for d in cursor],

        "total": total,

        "page": page,

        "page_size": page_size

    }



@app.patch("/attendance/{emp_code}/{date}")

def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})

    if not record:

        raise HTTPException(404, "Record not found")

    emp = db.employees.find_one({"emp_code": emp_code})



    new_status = body.status or record["status"]

    if new_status in ["ABSENT", "LEAVE"] and (body.punch_in or body.punch_out):

        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")



    new_in = datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc) if body.punch_in else record.get("punch_in")

    new_out = datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc) if body.punch_out else record.get("punch_out")



    if new_status in ["PRESENT", "WFH", "ON_DUTY"] and not new_in:

        raise HTTPException(422, "Presence status requires punch_in")



    if new_in and new_out:

        if (new_out - new_in).total_seconds() <= 0 or (new_out - new_in).total_seconds() > 86400:

            raise HTTPException(422, "Invalid punch duration")



    changes = {}

    if body.status and body.status != record["status"]:

        changes["status"] = {"from": record["status"], "to": body.status}



    late_m = compute_late_minutes(new_in, emp["shift_start"], date) if new_in else 0

    work_h = compute_work_hours(new_in, new_out) if new_in and new_out else None

    ot_m = compute_overtime_minutes(new_out, emp["shift_start"], emp["shift_end"], date) if new_out else 0

    half_d = (work_h < 4.50) if work_h is not None else False



    if new_in != record.get("punch_in"):

        changes["punch_in"] = {"from": record.get("punch_in"), "to": new_in}

    if new_out != record.get("punch_out"):

        changes["punch_out"] = {"from": record.get("punch_out"), "to": new_out}



    if not changes:

        raise HTTPException(422, "Request made no changes")



    history_entry = {

        "at": truncate_to_seconds(datetime.now(timezone.utc)),

        "by": body.regularized_by,

        "reason": body.reason,

        "changes": changes

    }



    updated = db.attendance_logs.find_one_and_update(

        {"_id": record["_id"]},

        {

            "$set": {

                "status": new_status,

                "punch_in": new_in,

                "punch_out": new_out,

                "work_hours": work_h,

                "late_minutes": late_m,

                "overtime_minutes": ot_m,

                "half_day": half_d

            },

            "$push": {"history": history_entry}

        },

        return_document=ReturnDocument.AFTER

    )

    return serialize_log(updated)



# --- Analytics Endpoints ---

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:

        raise HTTPException(404, "Employee not found")



    y, m = map(int, month.split("-"))

    _, num_days = calendar.monthrange(y, m)

    joined = date.fromisoformat(emp["joined_on"])



    working_days = 0

    for day in range(1, num_days + 1):

        cur_date = date(y, m, day)

        if cur_date >= joined and cur_date.weekday() < 5:

            working_days += 1



    logs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$regex": f"^{month}"}}))

    present_days = 0.0

    leave_days = 0

    late_count = 0

    tot_late = 0

    tot_ot = 0



    for l in logs:

        cur_d = date.fromisoformat(l["date"])

        is_weekday = cur_d.weekday() < 5

        st = l.get("status")

        if st in ["PRESENT", "WFH", "ON_DUTY"] and is_weekday:

            present_days += 0.5 if l.get("half_day") else 1.0

        elif st == "LEAVE":

            leave_days += 1



        lm = l.get("late_minutes", 0)

        if lm > 0:

            late_count += 1

            tot_late += lm

        tot_ot += l.get("overtime_minutes", 0)



    att_pct = round_half_up((present_days / working_days) * 100.0, 2) if working_days > 0 else None



    return {

        "emp_code": emp_code,

        "month": month,

        "working_days": working_days,

        "present_days": present_days,

        "leave_days": leave_days,

        "late_count": late_count,

        "total_late_minutes": tot_late,

        "total_overtime_minutes": tot_ot,

        "attendance_pct": att_pct

    }



@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):

    pipeline = [

        {"$match": {"joined_on": {"$lte": f"{month}-31"}}},

    ]

    if department:

        pipeline[0]["$match"]["department"] = department



    pipeline.extend([

        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "employees": {"$push": "$emp_code"}}},

        {"$sort": {"_id": ASCENDING}}

    ])



    dept_groups = list(db.employees.aggregate(pipeline))

    items = []

    for g in dept_groups:

        d_name = g["_id"]

        hc = g["headcount"]

        if hc == 0:

            continue

        emp_codes = g["employees"]



        logs = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$regex": f"^{month}"}}))

        pres_days = 0.0

        work_hours_list = []

        late_c = 0

        tot_late = 0

        leave_c = 0

        on_duty_c = 0



        for l in logs:

            st = l.get("status")

            cur_d = date.fromisoformat(l["date"])

            if st in ["PRESENT", "WFH", "ON_DUTY"] and cur_d.weekday() < 5:

                pres_days += 0.5 if l.get("half_day") else 1.0

            if st in ["PRESENT", "WFH", "ON_DUTY"] and l.get("work_hours") is not None:

                work_hours_list.append(l["work_hours"])

            if st == "LEAVE":

                leave_c += 1

            if st == "ON_DUTY":

                on_duty_c += 1

            lm = l.get("late_minutes", 0)

            if lm > 0:

                late_c += 1

                tot_late += lm



        avg_wh = round_half_up(sum(work_hours_list) / len(work_hours_list), 2) if work_hours_list else None



        items.append({

            "department": d_name,

            "headcount": hc,

            "present_days": pres_days,

            "avg_work_hours": avg_wh,

            "late_count": late_c,

            "total_late_minutes": tot_late,

            "leave_count": leave_c,

            "on_duty_count": on_duty_c

        })



    return {"month": month, "items": items}



@app.get("/analytics/leaderboard/late")

def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = 10, department: Optional[str] = None):

    pipeline = [

        {"$match": {"date": {"$regex": f"^{month}"}, "late_minutes": {"$gt": 0}}},

        {"$group": {

            "_id": "$emp_code",

            "total_late_minutes": {"$sum": "$late_minutes"},

            "late_count": {"$sum": 1}

        }},

        {"$lookup": {

            "from": "employees",

            "localField": "_id",

            "foreignField": "emp_code",

            "as": "emp"

        }},

        {"$unwind": "$emp"}

    ]

    if department:

        pipeline.append({"$match": {"emp.department": department}})



    pipeline.append({"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}})

    records = list(db.attendance_logs.aggregate(pipeline))



    items = []

    current_rank = 1

    for i, r in enumerate(records):

        if i > 0 and r["total_late_minutes"] < records[i - 1]["total_late_minutes"]:

            current_rank = i + 1

        if current_rank <= limit:

            items.append({

                "rank": current_rank,

                "emp_code": r["_id"],

                "name": r["emp"]["name"],

                "department": r["emp"]["department"],

                "total_late_minutes": r["total_late_minutes"],

                "late_count": r["late_count"]

            })



    return {"month": month, "items": items}



@app.get("/analytics/departments/{department}/trend")

def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):

    d_from = date.fromisoformat(from_date)

    d_to = date.fromisoformat(to_date)

    if d_to < d_from or (d_to - d_from).days > 92:

        raise HTTPException(422, "Invalid date range")



    emps = list(db.employees.find({"department": department}))

    if not emps:

        raise HTTPException(404, "Unknown department")



    emp_codes = [e["emp_code"] for e in emps]

    logs = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date, "$lte": to_date}}))



    log_map = {}

    for l in logs:

        log_map.setdefault(l["date"], []).append(l)



    items = []

    rates = []

    cur = d_from

    while cur <= d_to:

        ds = cur.isoformat()

        is_wd = cur.weekday() < 5

        hc = sum(1 for e in emps if date.fromisoformat(e["joined_on"]) <= cur)



        day_logs = log_map.get(ds, [])

        pres = sum(0.5 if l.get("half_day") else 1.0 for l in day_logs if l.get("status") in ["PRESENT", "WFH", "ON_DUTY"])

        late = sum(1 for l in day_logs if l.get("late_minutes", 0) > 0)



        rate = round_half_up(pres / hc, 4) if (is_wd and hc > 0) else None

        rates.append(rate)



        window = [r for r in rates[-7:] if r is not None]

        m_avg = round_half_up(sum(window) / len(window), 4) if window else None



        items.append({

            "date": ds,

            "is_working_day": is_wd,

            "headcount": hc,

            "present_count": pres,

            "late_count": late,

            "attendance_rate": rate,

            "moving_avg_7d": m_avg

        })

        cur += timedelta(days=1)



    return {"department": department, "items": items}



@app.get("/admin/explain/{endpoint}")

def explain_endpoint(endpoint: str):

    if endpoint == "attendance_list":

        plan = db.attendance_logs.find().sort([("date", DESCENDING), ("emp_code", ASCENDING)]).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":

        plan = db.attendance_logs.find({"emp_code": "EMP0001"}).explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    else:

        plan = db.attendance_logs.find().explain()

        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}


