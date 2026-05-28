#!/usr/bin/env python3
"""Verify the chat assistant config gate + (when configured) a real response.
Run against the LIVE backend:
  docker exec -w /app edge_backend python -m scripts.verify_chat

- /support/chat/status reports configured based on ANTHROPIC_API_KEY.
- NOT configured: POST /chat -> 503 'being configured' (widget gates on this).
- Configured (after bash /root/set_anthropic_key.sh): POST /chat streams a real
  assistant response (asserts non-empty text).
"""
import asyncio
import json
import httpx
from sqlalchemy import select
from app.database import async_session_factory
from app.models.user import User
from app.core.security import create_access_token

BASE = "http://localhost:8000"


async def _tok():
    async with async_session_factory() as db:
        u = (await db.execute(select(User).where(User.email == "pytest-fixture@thetaalgos.test"))).scalar_one()
        return create_access_token({"sub": str(u.id)})


def main():
    tok = asyncio.run(_tok())
    h = {"Authorization": f"Bearer {tok}"}
    with httpx.Client(base_url=BASE, headers=h, timeout=30.0) as c:
        st = c.get("/api/v1/support/chat/status")
        if st.status_code == 404:
            print("  [WARN] /chat/status not deployed on this server yet (branch not merged/restarted)")
            return
        assert st.status_code == 200, st.text
        configured = bool(st.json().get("configured"))
        print(f"  [PASS] /chat/status -> configured={configured}")

        if not configured:
            r = c.post("/api/v1/support/chat", json={"messages": [{"role": "user", "content": "hi"}]})
            ok = r.status_code == 503 and "being configured" in r.text
            print(f"  [{'PASS' if ok else 'FAIL'}] not-configured -> 503 'being configured' (got {r.status_code})")
            print("  [INFO] run 'bash /root/set_anthropic_key.sh' then re-run for the real-response check")
            return

        text = ""
        with c.stream("POST", "/api/v1/support/chat",
                      json={"messages": [{"role": "user", "content": "In one sentence, what is the Theta Scanner?"}]}) as r:
            assert r.status_code == 200, r.status_code
            for line in r.iter_lines():
                line = line.decode() if isinstance(line, bytes) else line
                if not line or not line.startswith("data: "):
                    continue
                p = json.loads(line[6:])
                if p.get("delta"):
                    text += p["delta"]
                if p.get("error"):
                    print(f"  [FAIL] provider error: {p['error']}"); return
                if p.get("done"):
                    break
        ok = len(text.strip()) > 0
        print(f"  [{'PASS' if ok else 'FAIL'}] configured -> real streamed response ({len(text)} chars): {text[:80]!r}")


if __name__ == "__main__":
    main()
