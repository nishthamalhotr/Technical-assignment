# Employee Attendance & Analytics API

FastAPI and MongoDB API for employee attendance, manual corrections with audit history, and attendance analytics. Requires Python 3.11+ and MongoDB 6.0+.

## Run locally

In PowerShell, from the project root:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API creates its indexes at startup. Open [Swagger UI](http://127.0.0.1:8000/docs) to explore the endpoints; `/health` checks MongoDB connectivity.

Set `MONGO_URI` and `MONGO_DB` in the environment for a different MongoDB instance. Do not commit credentials or `.env` files.
