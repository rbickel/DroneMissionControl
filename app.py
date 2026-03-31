import json
import math
import threading
from contextlib import asynccontextmanager
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


class Storm(BaseModel):
    id: str
    name: str = Field(..., description="Human-readable storm name")
    lat: float = Field(..., ge=-90, le=90, description="Eye latitude")
    lon: float = Field(..., ge=-180, le=180, description="Eye longitude")
    speed: float = Field(0, ge=0, description="Translational speed in m/s")
    direction: float = Field(0, ge=0, le=359.999, description="Movement heading in degrees")
    wind_speed_kmh: float = Field(0, ge=0, description="Max sustained wind speed in km/h")
    category: int = Field(1, ge=1, le=5, description="Saffir-Simpson category 1-5")
    eye_radius_km: float = Field(30, ge=5, description="Eye radius in km")
    inner_band_radius_km: float = Field(150, ge=20, description="Inner rain-band radius in km")
    outer_band_radius_km: float = Field(400, ge=50, description="Outer storm-band radius in km")


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


# Mount MCP server at /mcp
from mcp_server import mcp as drone_mcp

_mcp_http_app = drone_mcp.streamable_http_app()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    seed_data()
    async with drone_mcp.session_manager.run():
        yield
    stop_updater()


app = FastAPI(title="Drone Management API", version="1.0.0", lifespan=_lifespan)

# Expose MCP at /mcp by forwarding to the streamable HTTP app's internal handler
_mcp_route = _mcp_http_app.routes[0]  # the /mcp route from the Starlette sub-app
app.routes.insert(0, _mcp_route)


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

# In-memory store of drones and storms
DRONES: Dict[str, Drone] = {}
STORMS: Dict[str, Storm] = {}
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
            z-index: 1000;
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
        .storm-label {
            background: rgba(180, 0, 0, 0.85);
            color: #fff;
            font-size: 0.8rem;
            padding: 5px 8px;
            border-radius: 4px;
            border: 1px solid rgba(255, 80, 80, 0.5);
            font-weight: bold;
        }
        .arrow {
            display: inline-block;
            padding: 0 6px;
            font-size: 0.9rem;
            transform: translateY(-1px);
        }
        .drone-icon-wrapper {
            background: none !important;
            border: none !important;
        }
        .drone-icon-wrapper img {
            display: block;
            width: 42px;
            height: 42px;
            transform-origin: 50% 50%;
        }
        @keyframes storm-pulse {
            0% { opacity: 0.35; }
            50% { opacity: 0.55; }
            100% { opacity: 0.35; }
        }
        @keyframes storm-spin {
            from { transform: translate(-50%, -50%) rotate(0deg); }
            to   { transform: translate(-50%, -50%) rotate(360deg); }
        }
        .storm-eye-icon {
            background: none !important;
            border: none !important;
        }
        .storm-spiral {
            width: 60px;
            height: 60px;
            transform-origin: 50% 50%;
            animation: storm-spin 3s linear infinite;
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
        const DRONE_ICON_URL = 'https://cdn-icons-png.flaticon.com/512/1596/1596423.png';

        function createDroneIcon(direction) {
            const normalized = Number.isFinite(direction) ? direction : 0;
            const rotation = normalized - 45; // Base image faces north-east (45°)
            return L.divIcon({
                html: `<img src="${DRONE_ICON_URL}" style="transform: rotate(${rotation}deg);" alt="drone" />`,
                className: 'drone-icon-wrapper',
                iconSize: [42, 42],
                iconAnchor: [21, 21],
                popupAnchor: [0, -20],
            });
        }

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
                        marker.setIcon(createDroneIcon(dirVal));
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
                        const marker = L.marker([latVal, lonVal], { icon: createDroneIcon(dirVal) }).addTo(map).bindPopup(popupContent);
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

        // --- Storm rendering ---
        const stormLayers = {};

        function kmToMeters(km) { return km * 1000; }

        function getCategoryColor(cat) {
            const colors = {
                1: '#FFD700',
                2: '#FFA500',
                3: '#FF4500',
                4: '#DC143C',
                5: '#8B0000',
            };
            return colors[cat] || '#FF4500';
        }

        function createStormSpiralSvg(color) {
            return `<svg class="storm-spiral" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
                <path d="M50,50 Q50,20 30,15 Q10,10 15,35 Q20,60 50,50" fill="none" stroke="${color}" stroke-width="3" opacity="0.9"/>
                <path d="M50,50 Q50,80 70,85 Q90,90 85,65 Q80,40 50,50" fill="none" stroke="${color}" stroke-width="3" opacity="0.9"/>
                <path d="M50,50 Q20,50 15,30 Q10,10 35,15 Q60,20 50,50" fill="none" stroke="${color}" stroke-width="2.5" opacity="0.7"/>
                <path d="M50,50 Q80,50 85,70 Q90,90 65,85 Q40,80 50,50" fill="none" stroke="${color}" stroke-width="2.5" opacity="0.7"/>
                <circle cx="50" cy="50" r="6" fill="${color}" opacity="0.8"/>
                <circle cx="50" cy="50" r="3" fill="white" opacity="0.9"/>
            </svg>`;
        }

        async function refreshStorms() {
            try {
                const response = await fetch('/storms');
                if (!response.ok) return;
                const storms = await response.json();

                // Remove layers for storms no longer present
                const currentIds = new Set(storms.map(s => s.id));
                Object.keys(stormLayers).forEach(id => {
                    if (!currentIds.has(id)) {
                        stormLayers[id].forEach(layer => map.removeLayer(layer));
                        delete stormLayers[id];
                    }
                });

                storms.forEach(storm => {
                    const color = getCategoryColor(storm.category);

                    // Remove old layers for this storm
                    if (stormLayers[storm.id]) {
                        stormLayers[storm.id].forEach(layer => map.removeLayer(layer));
                    }

                    const layers = [];

                    // Outer band
                    const outer = L.circle([storm.lat, storm.lon], {
                        radius: kmToMeters(storm.outer_band_radius_km),
                        color: color,
                        weight: 1.5,
                        fillColor: color,
                        fillOpacity: 0.08,
                        dashArray: '8 6',
                        interactive: false,
                    }).addTo(map);
                    layers.push(outer);

                    // Inner band
                    const inner = L.circle([storm.lat, storm.lon], {
                        radius: kmToMeters(storm.inner_band_radius_km),
                        color: color,
                        weight: 2,
                        fillColor: color,
                        fillOpacity: 0.15,
                        dashArray: '5 4',
                        interactive: false,
                    }).addTo(map);
                    layers.push(inner);

                    // Danger zone (eye wall)
                    const eyeWall = L.circle([storm.lat, storm.lon], {
                        radius: kmToMeters(storm.eye_radius_km * 2.5),
                        color: color,
                        weight: 2.5,
                        fillColor: color,
                        fillOpacity: 0.25,
                        interactive: false,
                    }).addTo(map);
                    layers.push(eyeWall);

                    // Eye
                    const eye = L.circle([storm.lat, storm.lon], {
                        radius: kmToMeters(storm.eye_radius_km),
                        color: '#fff',
                        weight: 2,
                        fillColor: '#222',
                        fillOpacity: 0.3,
                        interactive: false,
                    }).addTo(map);
                    layers.push(eye);

                    // Spinning icon at center
                    const spiralIcon = L.divIcon({
                        html: createStormSpiralSvg(color),
                        className: 'storm-eye-icon',
                        iconSize: [60, 60],
                        iconAnchor: [30, 30],
                    });
                    const centerMarker = L.marker([storm.lat, storm.lon], { icon: spiralIcon, interactive: true }).addTo(map);

                    const arrow = getBearingArrow(storm.direction);
                    const popupHtml = `<strong>\\u{1F300} ${storm.name}</strong><br/>`
                        + `Category: <strong>${storm.category}</strong> (Saffir-Simpson)<br/>`
                        + `Winds: <strong>${storm.wind_speed_kmh.toFixed(0)} km/h</strong><br/>`
                        + `Position: ${storm.lat.toFixed(4)}°N, ${storm.lon.toFixed(4)}°E<br/>`
                        + `Moving: ${storm.direction.toFixed(0)}° ${arrow} at ${(storm.speed * 3.6).toFixed(1)} km/h<br/>`
                        + `Eye: ${storm.eye_radius_km} km &middot; Inner: ${storm.inner_band_radius_km} km &middot; Outer: ${storm.outer_band_radius_km} km`;
                    centerMarker.bindPopup(popupHtml);

                    const tooltipHtml = `\\u{1F300} ${storm.name} (Cat ${storm.category})<br/>`
                        + `${storm.wind_speed_kmh.toFixed(0)} km/h winds ${arrow}`;
                    centerMarker.bindTooltip(tooltipHtml, {
                        permanent: true,
                        direction: 'top',
                        offset: [0, -35],
                        className: 'storm-label',
                    });
                    layers.push(centerMarker);

                    // Direction indicator line (projected path ~ 200km)
                    const pathLen = 200;
                    const bearingRad = storm.direction * Math.PI / 180;
                    const dLat = (pathLen / 111.111) * Math.cos(bearingRad);
                    const dLon = (pathLen / (111.111 * Math.cos(storm.lat * Math.PI / 180))) * Math.sin(bearingRad);
                    const projLine = L.polyline(
                        [[storm.lat, storm.lon], [storm.lat + dLat, storm.lon + dLon]],
                        { color: color, weight: 3, dashArray: '10 8', opacity: 0.7, interactive: false }
                    ).addTo(map);
                    layers.push(projLine);

                    stormLayers[storm.id] = layers;
                });
            } catch (error) {
                console.error('Failed to refresh storms', error);
            }
        }

        refreshDrones();
        refreshStorms();
        setInterval(refreshDrones, 1000);
        setInterval(refreshStorms, 1000);
    </script>
</body>
</html>
"""


@app.get("/openapi.json", include_in_schema=False)
def get_openapi_spec(request: Request):
    """Serve the generated OpenAPI spec with host-specific server metadata."""
    schema = _custom_openapi(request)
    return Response(content=json.dumps(schema, indent=2), media_type="application/json")


def stop_updater():
    global POSITION_THREAD
    POSITION_THREAD_STOP.set()
    if POSITION_THREAD and POSITION_THREAD.is_alive():
        POSITION_THREAD.join(timeout=2.0)
    POSITION_THREAD = None


def _update_positions_loop():
    """Advance drone positions at a fixed cadence based on heading and speed."""
    while not POSITION_THREAD_STOP.wait(UPDATE_INTERVAL_SECONDS):
        # Update drone positions
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

        # Update storm positions
        for storm_id, storm in list(STORMS.items()):
            if storm.speed <= 0:
                continue

            distance = storm.speed * UPDATE_INTERVAL_SECONDS
            bearing_rad = math.radians(storm.direction)
            delta_lat = (distance * math.cos(bearing_rad)) / METERS_PER_DEGREE_LAT
            meters_per_degree_lon = METERS_PER_DEGREE_LAT * max(math.cos(math.radians(storm.lat)), 1e-6)
            delta_lon = (distance * math.sin(bearing_rad)) / meters_per_degree_lon

            storm.lat = max(min(storm.lat + delta_lat, 90.0), -90.0)
            storm.lon = (storm.lon + delta_lon + 180.0) % 360.0 - 180.0
            STORMS[storm_id] = storm


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


@app.get("/storms", response_model=List[Storm])
def list_storms():
    """Return all active storms with position, speed, wind, and radii."""
    return list(STORMS.values())


@app.get("/storms/{storm_id}", response_model=Storm)
def get_storm(storm_id: str):
    """Return a single storm by ID."""
    storm = STORMS.get(storm_id)
    if not storm:
        raise HTTPException(status_code=404, detail="Storm not found")
    return storm


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


def _seed_storms():
    """Seed a demo typhoon for the mission control scenario."""
    storms = [
        Storm(
            id="typhoon-hailong",
            name="Typhoon Hailong",
            lat=45.0,
            lon=-5.0,
            speed=7.5,
            direction=42.0,
            wind_speed_kmh=195.0,
            category=3,
            eye_radius_km=35,
            inner_band_radius_km=150,
            outer_band_radius_km=450,
        ),
    ]
    for s in storms:
        STORMS[s.id] = s


def seed_data():
    # Seed a few drones for demo
    initial = [
        Drone(id="Skythorn", lat=52.5200, lon=13.4050, speed=45.0, direction=120.0, base_lat=52.3667, base_lon=13.5033),
        Drone(id="Windlark", lat=48.1351, lon=11.5820, speed=60.0, direction=210.0, base_lat=48.3538, base_lon=11.7861),
        Drone(id="Riverstone", lat=53.5511, lon=9.9937, speed=65.0, direction=300.0, base_lat=53.6294, base_lon=9.9882),
        Drone(id="Frostbyte", lat=48.8566, lon=2.3522, speed=55.0, direction=140.0, base_lat=48.7262, base_lon=2.3652),
        Drone(id="Dawnstar", lat=45.7640, lon=4.8357, speed=52.0, direction=185.0, base_lat=45.7256, base_lon=4.9443),
        Drone(id="Cloudsong", lat=43.2965, lon=5.3698, speed=47.0, direction=95.0, base_lat=43.4393, base_lon=5.2228),
        Drone(id="Brightwing", lat=51.5074, lon=-0.1278, speed=58.0, direction=60.0, base_lat=51.4700, base_lon=-0.4543),
        Drone(id="Stormfallow", lat=53.4808, lon=-2.2426, speed=50.0, direction=310.0, base_lat=53.3650, base_lon=-2.2790),
        Drone(id="Emberleaf", lat=52.3676, lon=4.9041, speed=49.0, direction=45.0, base_lat=52.3105, base_lon=4.7683),
        Drone(id="Silvercrest", lat=50.8503, lon=4.3517, speed=53.0, direction=75.0, base_lat=50.9010, base_lon=4.4844),
        Drone(id="Moonbeam", lat=47.3769, lon=8.5417, speed=57.0, direction=25.0, base_lat=47.4647, base_lon=8.5492),
        Drone(id="Sunflare", lat=40.4168, lon=-3.7038, speed=62.0, direction=220.0, base_lat=40.4983, base_lon=-3.5676),
        Drone(id="Harborlight", lat=38.7223, lon=-9.1393, speed=48.0, direction=250.0, base_lat=38.7742, base_lon=-9.1342),
        Drone(id="Starfall", lat=53.3498, lon=-6.2603, speed=54.0, direction=15.0, base_lat=53.4213, base_lon=-6.2701),
        Drone(id="Everglow", lat=59.9139, lon=10.7522, speed=46.0, direction=330.0, base_lat=60.1976, base_lon=11.1004),
    ]
    for d in initial:
        DRONES[d.id] = d

    _seed_storms()
    _start_position_thread()
