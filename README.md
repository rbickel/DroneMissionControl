Drone Management REST API

Overview

This FastAPI service manages a fleet of simulated drones and provides:

- GET `/` – Leaflet map that refreshes every second with drone locations, speeds, and heading arrows.
- GET `/drones` – JSON list of all drones (id, lat, lon, speed, direction, base coordinates).
- PATCH `/drones/{id}/speed` – Update speed (m/s).
- PATCH `/drones/{id}/coordinates` – Provide target lat/lon; service recalculates heading automatically.
- POST `/drones/{id}/return-to-base` – Point drone back to its base; optionally override base `[lat, lon]`.
- POST `/drones` – Register a new drone.
- DELETE `/drones/{id}` – Remove a drone.
- GET `/swagger` – Swagger UI backed by `/openapi.json`.

Docs & tooling

- OpenAPI schema at `/openapi.json` includes the caller’s host in `servers`.
- Swagger UI (`/swagger`) provides interactive testing.

Quick start (Windows PowerShell)

```powershell
# From repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run the API
uvicorn app:app --reload --host 0.0.0.0 --port 8000

# Try it
curl http://localhost:8000/drones
# Open live map
start http://localhost:8000/
```

API examples

Create a drone:

```powershell
curl -X POST http://localhost:8000/drones `
  -H "Content-Type: application/json" `
  -d '{
    "id": "drone-99",
    "lat": 34.05,
    "lon": -118.25,
    "base_lat": 33.94,
    "base_lon": -118.40,
    "speed": 5,
    "direction": 270
  }'
```

Change speed:

```powershell
curl -X PATCH http://localhost:8000/drones/drone-99/speed `
  -H "Content-Type: application/json" `
  -d '{"speed": 12}'
```

Fly toward new coordinates:

```powershell
curl -X PATCH http://localhost:8000/drones/drone-99/coordinates `
  -H "Content-Type: application/json" `
  -d '{"lat": 34.20, "lon": -118.15}'
```

Return to base (optional override):

```powershell
curl -X POST http://localhost:8000/drones/drone-99/return-to-base `
  -H "Content-Type: application/json" `
  -d '{"base": [33.94, -118.40]}'
```

Notes

- Data is stored in-memory and seeded on startup with 15 demo drones distributed across Western Europe; restart to reset state.
- A background thread advances drone positions every second using their speed and heading (simple planar approximation).
- Heading recalculation happens automatically when supplying destination coordinates or returning to base; computations rely on a great-circle bearing approximation and do not handle navigation constraints or obstacle avoidance.
- Speed is a scalar in m/s; no advanced physics are applied beyond straight-line motion.
