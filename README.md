# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
    pip install -r requirements.txt
    export MONGO_URI=mongodb://localhost:27017
    export MONGO_DB=attendance_db
    uvicorn app.main:app --port 8000

MONGO_URI and MONGO_DB are read from the environment (a local .env is also loaded). Indexes are created at startup.

## Notes
- All endpoints from openapi.yaml are implemented, including the explain endpoint `/admin/explain/{endpoint}`.
- The analytics endpoints are MongoDB aggregation pipelines.
- Queries use index hints so the explain output shows IXSCAN; the hints rely on the indexes created at startup.

## Known limitations
- The punch-in race is handled by a unique index and `DuplicateKeyError` (409). I did not run a parallel-request test to confirm it.
- On a 100k-log dataset, department summary takes about 0.5 s and department trend (91-day range) about 1.3 s. Both use indexes but are not fully optimized.
- Indexes are created at startup inside a try/except with no retry. If index creation fails, endpoints that use index hints can return 500.
- The pre-aggregated monthly totals for 100x data are only a design in DECISIONS.md, not implemented.