"""
MCP Server for the Drone Management API.

Exposes drone fleet operations as MCP tools so that LLM-based agents
can list, create, command, and remove drones via natural-language workflows.

The MCP endpoint is mounted at /mcp on the main FastAPI app.
It can also be run standalone for stdio/SSE transports.

Usage (standalone):
    python mcp_server.py                         # stdio transport (default)
    python mcp_server.py --transport sse          # SSE transport on port 8001
    python mcp_server.py --api-url http://host:port  # custom API URL
"""

import argparse
import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_API_URL = "http://localhost:8000"

# Build the allowed-hosts list from the ALLOWED_MCP_HOSTS env var (comma-separated).
# When empty the MCP SDK's DNS-rebinding protection is disabled so the server
# works behind any reverse-proxy (Azure Container Apps, etc.) without extra config.
_allowed_hosts = [h.strip() for h in os.environ.get("ALLOWED_MCP_HOSTS", "").split(",") if h.strip()]

mcp = FastMCP(
    "Drone Mission Control",
    instructions=(
        "You are DroneOps Copilot, an AI assistant who helps operators manage "
        "a fleet of drones via MCP tools. Use the available tools to list drones, "
        "change speed or heading, send drones to coordinates, return them to base, "
        "create new drones, or remove existing ones. Coordinates are in decimal "
        "degrees (lat/lon). Speed is in m/s. Heading is 0-360 degrees."
    ),
    # Disable DNS-rebinding protection when no explicit hosts are configured,
    # since the app runs behind Azure Container Apps' own ingress layer.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=len(_allowed_hosts) > 0,
        allowed_hosts=_allowed_hosts,
    ),
)

_api_url: str = DEFAULT_API_URL


def _url(path: str) -> str:
    return f"{_api_url.rstrip('/')}{path}"


async def _api_get(path: str) -> Any:
    async with httpx.AsyncClient() as client:
        resp = await client.get(_url(path), timeout=10.0)
        resp.raise_for_status()
        return resp.json()


async def _api_patch(path: str, payload: dict) -> Any:
    async with httpx.AsyncClient() as client:
        resp = await client.patch(_url(path), json=payload, timeout=10.0)
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, payload: dict | None = None) -> Any:
    async with httpx.AsyncClient() as client:
        resp = await client.post(_url(path), json=payload or {}, timeout=10.0)
        resp.raise_for_status()
        return resp.json()


async def _api_delete(path: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.delete(_url(path), timeout=10.0)
        resp.raise_for_status()
        return "Deleted successfully."


def _format_drone(d: dict) -> str:
    return (
        f"Drone {d['id']}: lat={d['lat']:.4f}, lon={d['lon']:.4f}, "
        f"speed={d['speed']:.1f} m/s, heading={d['direction']:.1f}°, "
        f"base=({d['base_lat']:.4f}, {d['base_lon']:.4f}), "
        f"returning={'yes' if d.get('return_to_base') else 'no'}"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_drones() -> str:
    """List all drones in the fleet with their current position, speed, heading, and base coordinates."""
    drones = await _api_get("/drones")
    if not drones:
        return "No drones in the fleet."
    return "\n".join(_format_drone(d) for d in drones)


@mcp.tool()
async def get_drone(drone_id: str) -> str:
    """Get details for a single drone by its ID.

    Args:
        drone_id: The unique identifier of the drone (e.g. "Skythorn").
    """
    drones = await _api_get("/drones")
    for d in drones:
        if d["id"] == drone_id:
            return _format_drone(d)
    return f"Drone '{drone_id}' not found."


@mcp.tool()
async def change_speed(drone_id: str, speed: float) -> str:
    """Change the speed of a drone.

    Args:
        drone_id: The unique identifier of the drone.
        speed: New speed in metres per second (>= 0). Set to 0 to hover.
    """
    result = await _api_patch(f"/drones/{drone_id}/speed", {"speed": speed})
    return _format_drone(result)


@mcp.tool()
async def send_drone_to_coordinates(drone_id: str, lat: float, lon: float) -> str:
    """Set the heading of a drone so it flies toward the given coordinates.
    This recalculates the bearing automatically; it does NOT change speed.

    Args:
        drone_id: The unique identifier of the drone.
        lat: Target latitude in decimal degrees (-90 to 90).
        lon: Target longitude in decimal degrees (-180 to 180).
    """
    result = await _api_patch(f"/drones/{drone_id}/coordinates", {"lat": lat, "lon": lon})
    return _format_drone(result)


@mcp.tool()
async def return_to_base(drone_id: str, base_lat: float | None = None, base_lon: float | None = None) -> str:
    """Command a drone to return to its base. Optionally override the base coordinates.

    Args:
        drone_id: The unique identifier of the drone.
        base_lat: Optional override latitude for the base.
        base_lon: Optional override longitude for the base.
    """
    payload: dict[str, Any] = {}
    if base_lat is not None and base_lon is not None:
        payload["base"] = [base_lat, base_lon]
    result = await _api_post(f"/drones/{drone_id}/return-to-base", payload)
    return _format_drone(result)


@mcp.tool()
async def create_drone(
    drone_id: str,
    lat: float,
    lon: float,
    base_lat: float,
    base_lon: float,
    speed: float = 0.0,
    direction: float = 0.0,
) -> str:
    """Register a new drone in the fleet.

    Args:
        drone_id: A unique name/identifier for the new drone.
        lat: Initial latitude in decimal degrees.
        lon: Initial longitude in decimal degrees.
        base_lat: Base latitude in decimal degrees.
        base_lon: Base longitude in decimal degrees.
        speed: Initial speed in m/s (default 0 = hovering).
        direction: Initial heading in degrees 0-360 (default 0 = north).
    """
    payload = {
        "id": drone_id,
        "lat": lat,
        "lon": lon,
        "base_lat": base_lat,
        "base_lon": base_lon,
        "speed": speed,
        "direction": direction,
    }
    result = await _api_post("/drones", payload)
    return f"Created: {_format_drone(result)}"


@mcp.tool()
async def delete_drone(drone_id: str) -> str:
    """Remove a drone from the fleet. This is irreversible.

    Args:
        drone_id: The unique identifier of the drone to delete.
    """
    msg = await _api_delete(f"/drones/{drone_id}")
    return f"Drone '{drone_id}' deleted. {msg}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP Server for Drone Mission Control")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base URL of the Drone API")
    parser.add_argument("--transport", choices=["stdio", "sse"], default="stdio", help="MCP transport")
    parser.add_argument("--port", type=int, default=8001, help="Port for SSE transport")
    args = parser.parse_args()

    _api_url = args.api_url

    if args.transport == "sse":
        mcp.run(transport="sse", port=args.port)
    else:
        mcp.run(transport="stdio")
