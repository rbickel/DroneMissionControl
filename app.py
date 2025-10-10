import json
import math
import threading
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field


class Drone(BaseModel):
    id: str
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    speed: float = Field(0, ge=0, description="Speed in m/s")
    direction: float = Field(0, ge=0, le=359.999, description="Bearing in degrees 0-360")
    base_lat: float = Field(..., ge=-90, le=90)
    base_lon: float = Field(..., ge=-180, le=180)
    return_to_base: bool = Field(False, description="Whether the drone is in return-to-base mode")


class DroneUpdateSpeed(BaseModel):
    speed: float = Field(..., ge=0, description="New speed in m/s")


class DroneUpdateCoordinates(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Target latitude")
    lon: float = Field(..., ge=-180, le=180, description="Target longitude")


class DroneReturnRequest(BaseModel):
    # Optionally allow overriding base for this return
    base: Optional[List[float]] = Field(
        default=None, description="[lat, lon] to return to; defaults to configured base"
    )


class DroneCreate(BaseModel):
    id: str
    lat: float
    lon: float
    base_lat: float
    base_lon: float
    speed: float = 0
    direction: float = 0


app = FastAPI(title="Drone Management API", version="1.0.0")


def _custom_openapi(request: Optional[Request] = None):
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description="Manage and track drones in real time.",
        routes=app.routes,
    )
    
    schema["servers"] = [
        {"url": "https://rbkl-drone-gps.lemonbush-02b762b9.westeurope.azurecontainerapps.io/", "description": "Production"},
        {"url": "http://localhost:8000", "description": "Local development"},
    ]
    
    if request is not None:
        base_url = str(request.base_url).rstrip("/")
        schema["servers"] = [{"url": base_url, "description": "Current host"}]

    return schema

app.openapi = lambda: _custom_openapi()

# In-memory store of drones
DRONES: Dict[str, Drone] = {}
UPDATE_INTERVAL_SECONDS = 1.0
METERS_PER_DEGREE_LAT = 111_111.0
POSITION_THREAD_STOP = threading.Event()
POSITION_THREAD: Optional[threading.Thread] = None


MAP_PAGE_HTML = """<!DOCTYPE html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <title>Drone Fleet Tracker</title>
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
    <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" crossorigin=\"\" />
    <style>
        html, body, #map { height: 100%; margin: 0; }
        .info-panel {
            position: absolute;
            top: 1rem;
            left: 1rem;
            background: rgba(255, 255, 255, 0.9);
            padding: 0.75rem 1rem;
            border-radius: 8px;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.2);
            font-family: Arial, sans-serif;
        }
        .info-panel h2 { margin: 0 0 0.5rem 0; font-size: 1.1rem; }
        .info-panel p { margin: 0; font-size: 0.9rem; }
        .drone-label {
            background: rgba(0, 17, 51, 0.75);
            color: #fff;
            font-size: 0.75rem;
            padding: 4px 6px;
            border-radius: 4px;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        .arrow {
            display: inline-block;
            padding: 0 6px;
            font-size: 0.9rem;
            transform: translateY(-1px);
        }
    </style>
</head>
<body>
    <div id=\"map\"></div>
    <div class=\"info-panel\">
        <h2>Drone Fleet</h2>
        <p>Tracking live positions over Germany.</p>
    </div>
    <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\" crossorigin=\"\"></script>
    <script>
        const map = L.map('map').setView([51.1657, 10.4515], 6);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19,
            attribution: '&copy; OpenStreetMap contributors'
        }).addTo(map);

        const markers = {};
        const droneIcon = L.icon({
            iconUrl: 'https://cdn-icons-png.flaticon.com/512/652/652526.png',
            iconSize: [42, 42],
            iconAnchor: [21, 21],
            popupAnchor: [0, -20]
        });

        function getBearingArrow(direction) {
            if (!Number.isFinite(direction)) {
                return '';
            }
            const headings = [
                { range: [337.5, 360], arrow: '↑' },
                { range: [0, 22.5], arrow: '↑' },
                { range: [22.5, 67.5], arrow: '↗' },
                { range: [67.5, 112.5], arrow: '→' },
                { range: [112.5, 157.5], arrow: '↘' },
                { range: [157.5, 202.5], arrow: '↓' },
                { range: [202.5, 247.5], arrow: '↙' },
                { range: [247.5, 292.5], arrow: '←' },
                { range: [292.5, 337.5], arrow: '↖' },
            ];
            const normalized = ((direction % 360) + 360) % 360;
            for (const h of headings) {
                const [start, end] = h.range;
                if (start <= normalized && normalized < end) {
                    return `<span class="arrow">${h.arrow}</span>`;
                }
            }
            return '';
        }

        async function refreshDrones() {
            try {
                const response = await fetch('/drones');
                if (!response.ok) {
                    return;
                }
                const drones = await response.json();
                drones.forEach((drone) => {
                    const latVal = Number(drone.lat);
                    const lonVal = Number(drone.lon);
                    const speedVal = Number(drone.speed);
                    const dirVal = Number(drone.direction);
                    if (!Number.isFinite(latVal) || !Number.isFinite(lonVal)) {
                        return;
                    }
                    const popupContent = `<strong>${drone.id}</strong><br/>Lat: ${latVal.toFixed(4)}<br/>Lon: ${lonVal.toFixed(4)}<br/>Speed: ${Number.isFinite(speedVal) ? speedVal.toFixed(1) : 'N/A'} m/s<br/>Direction: ${Number.isFinite(dirVal) ? dirVal.toFixed(1) : 'N/A'}°`;
                    const arrow = getBearingArrow(dirVal);
                    const tooltipContent = `${drone.id}: ${Number.isFinite(speedVal) ? speedVal.toFixed(1) : 'N/A'} m/s\n${latVal.toFixed(4)}, ${lonVal.toFixed(4)}\nDir: ${Number.isFinite(dirVal) ? dirVal.toFixed(1) : 'N/A'}° ${arrow}`;
                    const tooltipHtml = tooltipContent.split('\\n').join('<br/>');
                    if (markers[drone.id]) {
                        const marker = markers[drone.id];
                        marker.setLatLng([latVal, lonVal]).setPopupContent(popupContent);
                        if (marker.getTooltip()) {
                            marker.setTooltipContent(tooltipHtml);
                        } else {
                            marker.bindTooltip(tooltipHtml, {
                                permanent: true,
                                direction: 'right',
                                offset: [24, 0],
                                className: 'drone-label',
                            });
                        }
                    } else {
                        const marker = L.marker([latVal, lonVal], { icon: droneIcon }).addTo(map).bindPopup(popupContent);
                        marker.bindTooltip(tooltipHtml, {
                            permanent: true,
                            direction: 'right',
                            offset: [24, 0],
                            className: 'drone-label',
                        });
                        markers[drone.id] = marker;
                    }
                });
            } catch (error) {
                console.error('Failed to refresh drones', error);
            }
        }

        refreshDrones();
        setInterval(refreshDrones, 1000);
    </script>
</body>
</html>
"""


@app.get("/openapi.json", include_in_schema=False)
def get_openapi_spec(request: Request):
    """Serve the generated OpenAPI spec with host-specific server metadata."""
    schema = _custom_openapi(request)
    return Response(content=json.dumps(schema, indent=2), media_type="application/json")


@app.on_event("shutdown")
def stop_updater():
    global POSITION_THREAD
    POSITION_THREAD_STOP.set()
    if POSITION_THREAD and POSITION_THREAD.is_alive():
        POSITION_THREAD.join(timeout=2.0)
    POSITION_THREAD = None


def _update_positions_loop():
    """Advance drone positions at a fixed cadence based on heading and speed."""
    while not POSITION_THREAD_STOP.wait(UPDATE_INTERVAL_SECONDS):
        for drone_id, drone in list(DRONES.items()):
            if drone.speed <= 0:
                continue

            distance = drone.speed * UPDATE_INTERVAL_SECONDS
            bearing_rad = math.radians(drone.direction)
            delta_lat = (distance * math.cos(bearing_rad)) / METERS_PER_DEGREE_LAT
            meters_per_degree_lon = METERS_PER_DEGREE_LAT * max(math.cos(math.radians(drone.lat)), 1e-6)
            delta_lon = (distance * math.sin(bearing_rad)) / meters_per_degree_lon

            new_lat = max(min(drone.lat + delta_lat, 90.0), -90.0)
            new_lon = (drone.lon + delta_lon + 180.0) % 360.0 - 180.0

            drone.lat = new_lat
            drone.lon = new_lon
            DRONES[drone_id] = drone


def _start_position_thread():
    """Ensure the background updater thread is running."""
    global POSITION_THREAD
    if POSITION_THREAD is None or not POSITION_THREAD.is_alive():
        POSITION_THREAD_STOP.clear()
        POSITION_THREAD = threading.Thread(target=_update_positions_loop, daemon=True)
        POSITION_THREAD.start()


def _bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return forward azimuth in degrees from point 1 to point 2."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    d_lon = math.radians(lon2 - lon1)
    x = math.sin(d_lon) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(d_lon)
    bearing = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    return bearing


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def root_page():
    return HTMLResponse(content=MAP_PAGE_HTML)


@app.get("/swagger", include_in_schema=False)
def swagger_ui():
    """Serve Swagger UI configured to use the generated spec."""
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Drone Management API - Swagger UI")


@app.get("/drones", response_model=List[Drone])
def list_drones():
    """Return all drones with position, speed, and direction."""
    return list(DRONES.values())


@app.patch("/drones/{drone_id}/speed", response_model=Drone)
def change_speed(drone_id: str, payload: DroneUpdateSpeed):
    drone = DRONES.get(drone_id)
    if not drone:
        raise HTTPException(status_code=404, detail="Drone not found")
    drone.speed = payload.speed
    drone.return_to_base = False
    DRONES[drone_id] = drone
    return drone


@app.patch("/drones/{drone_id}/coordinates", response_model=Drone)
def adjust_heading_to_coordinates(drone_id: str, payload: DroneUpdateCoordinates):
    drone = DRONES.get(drone_id)
    if not drone:
        raise HTTPException(status_code=404, detail="Drone not found")

    if math.isclose(drone.lat, payload.lat, abs_tol=1e-6) and math.isclose(drone.lon, payload.lon, abs_tol=1e-6):
        drone.return_to_base = False
        DRONES[drone_id] = drone
        return drone

    drone.direction = _bearing_between(drone.lat, drone.lon, payload.lat, payload.lon)
    drone.return_to_base = False
    DRONES[drone_id] = drone
    return drone


@app.post("/drones/{drone_id}/return-to-base", response_model=Drone)
def return_to_base(drone_id: str, payload: DroneReturnRequest = DroneReturnRequest()):
    drone = DRONES.get(drone_id)
    if not drone:
        raise HTTPException(status_code=404, detail="Drone not found")

    # Set destination to base and point direction toward base (simple arithmetic, not full navigation)
    if payload.base is not None:
        if len(payload.base) != 2:
            raise HTTPException(status_code=400, detail="Base must be [lat, lon]")
        base_lat, base_lon = payload.base
        drone.base_lat = base_lat
        drone.base_lon = base_lon

    # Naive direction: if same lon, north/south; if same lat, east/west; else keep current
    if abs(drone.lon - drone.base_lon) < 1e-6:
        drone.direction = 0.0 if drone.base_lat > drone.lat else 180.0
    elif abs(drone.lat - drone.base_lat) < 1e-6:
        drone.direction = 90.0 if drone.base_lon > drone.lon else 270.0
    else:
        # Compute bearing from current to base (approximate)
        lat1 = math.radians(drone.lat)
        lon1 = math.radians(drone.lon)
        lat2 = math.radians(drone.base_lat)
        lon2 = math.radians(drone.base_lon)
        d_lon = lon2 - lon1
        x = math.sin(d_lon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon)
        bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
        drone.direction = bearing

    # Optional: set a standard return speed if current speed is zero
    if drone.speed == 0:
        drone.speed = 10.0

    drone.return_to_base = True
    DRONES[drone_id] = drone
    return drone


@app.post("/drones", response_model=Drone, status_code=201)
def create_drone(payload: DroneCreate):
    if payload.id in DRONES:
        raise HTTPException(status_code=409, detail="Drone id already exists")
    drone = Drone(**payload.model_dump())
    DRONES[drone.id] = drone
    return drone


@app.delete("/drones/{drone_id}", status_code=204)
def delete_drone(drone_id: str):
    if drone_id not in DRONES:
        raise HTTPException(status_code=404, detail="Drone not found")
    del DRONES[drone_id]
    return None


@app.on_event("startup")
def seed_data():
    # Seed a few drones for demo
    initial = [
        Drone(id="Aquila", lat=52.5200, lon=13.4050, speed=45.0, direction=120.0, base_lat=52.3667, base_lon=13.5033),
        Drone(id="Valkyrie", lat=48.1351, lon=11.5820, speed=60.0, direction=210.0, base_lat=48.3538, base_lon=11.7861),
        Drone(id="Lupus", lat=53.5511, lon=9.9937, speed=65.0, direction=300.0, base_lat=53.6294, base_lon=9.9882),
        Drone(id="Orion", lat=48.8566, lon=2.3522, speed=55.0, direction=140.0, base_lat=48.7262, base_lon=2.3652),
        Drone(id="Nemesis", lat=45.7640, lon=4.8357, speed=52.0, direction=185.0, base_lat=45.7256, base_lon=4.9443),
        Drone(id="Aether", lat=43.2965, lon=5.3698, speed=47.0, direction=95.0, base_lat=43.4393, base_lon=5.2228),
        Drone(id="Cerberus", lat=51.5074, lon=-0.1278, speed=58.0, direction=60.0, base_lat=51.4700, base_lon=-0.4543),
        Drone(id="Hydra", lat=53.4808, lon=-2.2426, speed=50.0, direction=310.0, base_lat=53.3650, base_lon=-2.2790),
        Drone(id="Pegasus", lat=52.3676, lon=4.9041, speed=49.0, direction=45.0, base_lat=52.3105, base_lon=4.7683),
        Drone(id="Gryphon", lat=50.8503, lon=4.3517, speed=53.0, direction=75.0, base_lat=50.9010, base_lon=4.4844),
        Drone(id="Minerva", lat=47.3769, lon=8.5417, speed=57.0, direction=25.0, base_lat=47.4647, base_lon=8.5492),
        Drone(id="Phoenix", lat=40.4168, lon=-3.7038, speed=62.0, direction=220.0, base_lat=40.4983, base_lon=-3.5676),
        Drone(id="Atlas", lat=38.7223, lon=-9.1393, speed=48.0, direction=250.0, base_lat=38.7742, base_lon=-9.1342),
        Drone(id="Vortex", lat=53.3498, lon=-6.2603, speed=54.0, direction=15.0, base_lat=53.4213, base_lon=-6.2701),
        Drone(id="Odin", lat=59.9139, lon=10.7522, speed=46.0, direction=330.0, base_lat=60.1976, base_lon=11.1004),
    ]
    for d in initial:
        DRONES[d.id] = d

    _start_position_thread()
