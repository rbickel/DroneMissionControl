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

        # Adjust heading toward target coordinates
        r = await client.patch(
            f"{BASE}/drones/drone-99/coordinates",
            json={"lat": 34.20, "lon": -118.15},
        )
        coord_payload = r.json()
        print("PATCH /coordinates", r.status_code, coord_payload)

        # Spot check that the drone is ready to fly at mission speed
        r = await client.get(f"{BASE}/drones")
        drones = r.json()
        drone_entry = next(d for d in drones if d["id"] == "drone-99")
        print("POST-COORDINATES STATE", drone_entry)

        # Return to base
        r = await client.post(
            f"{BASE}/drones/drone-99/return-to-base",
            json={"base": [33.94, -118.40]},
        )
        print("POST /return-to-base", r.status_code, r.json())

        # Allow background updater to process the return sequence
        await asyncio.sleep(2)
        r = await client.get(f"{BASE}/drones")
        drones = r.json()
        drone_entry = next(d for d in drones if d["id"] == "drone-99")
        print("POST-RETURN STATE", drone_entry)

if __name__ == "__main__":
    asyncio.run(main())
