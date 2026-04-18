import asyncio
import os
from aiohttp import web

APP_STATE = {
    "race_state": {
        "updated_at": None,
        "meeting_name": "",
        "track_code": "",
        "track_name": "",
        "event_code": "",
        "event_name": "",
        "event_type": "",
        "session_status": "",
        "countdown_laps": 0,
        "elapsed_value": 0,
        "standings": [],
    }
}

INGEST_TOKEN = os.getenv("INGEST_TOKEN", "change-me-now")

HTML_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Natsoft Live Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {
      font-family: Arial, sans-serif;
      background: #111;
      color: #eee;
      margin: 20px;
    }
    h1, h2, p {
      margin: 0 0 10px 0;
    }
    .meta {
      margin-bottom: 20px;
      padding: 12px;
      background: #1b1b1b;
      border-radius: 8px;
    }
    .status {
      font-weight: bold;
    }
    .table-wrap {
      overflow-x: auto;
      background: #1b1b1b;
      border-radius: 8px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      padding: 10px 8px;
      border-bottom: 1px solid #2c2c2c;
      text-align: left;
      white-space: nowrap;
    }
    th {
      background: #222;
      position: sticky;
      top: 0;
    }
  </style>
</head>
<body>
  <div class="meta">
    <h1 id="meeting">Loading...</h1>
    <h2 id="event"></h2>
    <p id="track"></p>
    <p class="status" id="status"></p>
    <p id="updated"></p>
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Pos</th>
          <th>Car</th>
          <th>Driver</th>
          <th>Vehicle</th>
          <th>Laps</th>
          <th>Last Lap</th>
          <th>Best Lap</th>
          <th>Gap</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <script>
    async function loadLive() {
      try {
        const res = await fetch('/live?_=' + Date.now(), { cache: 'no-store' });
        const data = await res.json();

        document.getElementById('meeting').textContent = data.meeting_name || 'No meeting';
        document.getElementById('event').textContent = data.event_name || '';
        document.getElementById('track').textContent = (data.track_name || '') + ' (' + (data.track_code || '') + ')';
        document.getElementById('status').textContent = 'Status: ' + (data.session_status || '');
        document.getElementById('updated').textContent = 'Updated: ' + (data.updated_at || '');

        const rows = document.getElementById('rows');
        rows.innerHTML = '';

        for (const row of (data.standings || [])) {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${row.position ?? ''}</td>
            <td>${row.car ?? ''}</td>
            <td>${row.driver ?? ''}</td>
            <td>${row.vehicle ?? ''}</td>
            <td>${row.laps ?? ''}</td>
            <td>${Number(row.last_lap || 0).toFixed(4)}</td>
            <td>${Number(row.best_lap || 0).toFixed(4)}</td>
            <td>${row.gap_time || row.gap_laps || ''}</td>
          `;
          rows.appendChild(tr);
        }
      } catch (err) {
        console.error(err);
      }
    }

    loadLive();
    setInterval(loadLive, 250);
  </script>
</body>
</html>
"""


async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_dashboard(request: web.Request) -> web.Response:
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_live(request: web.Request) -> web.Response:
    return web.json_response(APP_STATE["race_state"])


async def handle_ingest(request: web.Request) -> web.Response:
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {INGEST_TOKEN}"

    if auth != expected:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    APP_STATE["race_state"] = payload
    return web.json_response({"ok": True})


async def start_api() -> None:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/live", handle_live)
    app.router.add_post("/ingest", handle_ingest)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Dashboard running on port {port}")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(start_api())
