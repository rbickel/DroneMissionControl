Drone Management REST API

Overview

This FastAPI app provides endpoints to manage drones:
- GET /drones: List all drones (id, position lat/lon, speed, direction, base)
- PATCH /drones/{id}/speed: Set the drone speed
- PATCH /drones/{id}/direction: Set the drone direction (0-360 degrees)
- POST /drones/{id}/return-to-base: Point drone back to its base; optional base override
- POST /drones: Create a drone
- DELETE /drones/{id}: Delete a drone

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

Change direction:

```powershell
curl -X PATCH http://localhost:8000/drones/drone-99/direction `
  -H "Content-Type: application/json" `
  -d '{"direction": 45}'
```

Return to base (optional override):

```powershell
curl -X POST http://localhost:8000/drones/drone-99/return-to-base `
  -H "Content-Type: application/json" `
  -d '{"base": [33.94, -118.40]}'
```

Notes

- Data is stored in-memory and seeded on startup with 3 demo drones.
- Direction calculation when returning to base uses a simple bearing formula; it does not simulate movement.
- Speed is a scalar in m/s; no physics are applied beyond setting values.