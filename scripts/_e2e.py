"""HIA-51 e2e - 直接运行"""
import asyncio
import httpx

BASE = "http://127.0.0.1:8000"

async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        r = await c.get("/health"); print(f"1.health {r.status_code} {r.json()}")
        r = await c.post("/api/auth/bootstrap"); print(f"2.bootstrap {r.status_code}")
        r = await c.post("/projects",
                          headers={"X-User-Email": "alice@test.com"},
                          json={"name": "Acme Quality"})
        print(f"3.create_project {r.status_code} {r.text[:300]}")
        if r.status_code != 201:
            return
        proj = r.json(); pid = proj["id"]

        r = await c.get(f"/projects/{pid}", headers={"X-User-Email": "bob@test.com"})
        print(f"4.bob_isolated {r.status_code}")

        r = await c.post(f"/projects/{pid}/members",
                          headers={"X-User-Email": "alice@test.com"},
                          json={"email": "bob@test.com", "role": "viewer"})
        print(f"5.invite_viewer {r.status_code}")

        r = await c.get(f"/projects/{pid}/use-cases", headers={"X-User-Email": "bob@test.com"})
        print(f"6.viewer_list {r.status_code}")

        r = await c.post(f"/projects/{pid}/use-cases",
                          headers={"X-User-Email": "bob@test.com"},
                          json={"name": "Bob UC"})
        print(f"7.viewer_cant_create {r.status_code}")

        # alice (OWNER) promotes bob to editor
        me = (await c.get("/api/users/me", headers={"X-User-Email": "bob@test.com"})).json()
        bob_id = me["user"]["id"]
        r = await c.patch(f"/projects/{pid}/members/{bob_id}",
                           headers={"X-User-Email": "alice@test.com"},
                           json={"role": "editor"})
        print(f"8.promote_editor {r.status_code}")

        r = await c.post(f"/projects/{pid}/use-cases",
                          headers={"X-User-Email": "bob@test.com"},
                          json={"name": "Batch Traceability"})
        print(f"9.editor_creates {r.status_code}")

        r = await c.get(f"/projects/{pid}/audit",
                         headers={"X-User-Email": "alice@test.com"})
        events = r.json()
        print(f"10.audit_count {len(events)}")
        print(f"11.audit_types {[e['event_type'] for e in events[:5]]}")

        r = await c.get("/audit/verify", params={"project_id": pid},
                         headers={"X-User-Email": "admin@ontolohub.local"})
        print(f"12.chain {r.json()}")

asyncio.run(main())
