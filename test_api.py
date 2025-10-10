import asyncio
import httpx

BASE="http://localhost:8000"

async def main():
    async with httpx.AsyncClient() as client:
        # List seeded drones
        r = await client.get(f"{BASE}/drones")
        print("GET /drones", r.status_code, r.json())

        # Create a drone
        r = await client.post(
            f"{BASE}/drones",
            json={
                "id": "drone-99",
                "lat": 34.05,
                "lon": -118.25,
                "base_lat": 33.94,
                "base_lon": -118.40,
                "speed": 5,
                "direction": 270,
            },
        )
        print("POST /drones", r.status_code, r.json())

        # Change speed
        r = await client.patch(f"{BASE}/drones/drone-99/speed", json={"speed": 12})
        print("PATCH /speed", r.status_code, r.json())

        # Change direction
        r = await client.patch(f"{BASE}/drones/drone-99/direction", json={"direction": 45})
        print("PATCH /direction", r.status_code, r.json())

        # Return to base
        r = await client.post(f"{BASE}/drones/drone-99/return-to-base", json={"base": [33.94, -118.40]})
        print("POST /return-to-base", r.status_code, r.json())

if __name__ == "__main__":
    asyncio.run(main())
