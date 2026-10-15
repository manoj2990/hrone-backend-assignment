# HROne Assignment

Employee Attendance & Analytics API built with FastAPI and MongoDB.

## Tech Stack

- Python
- FastAPI
- MongoDB (PyMongo)
- Pydantic
- Uvicorn

## Setup & Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

## Endpoints Covered

- `GET /health`
- `POST /employees`
- `GET /employees`
- `POST /attendance/punch-in`
- `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/leaderboard/late`

---

**Done by:** Manoj Meena