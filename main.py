import asyncio
import json
import os
import pathlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from aiohttp import web, ClientSession, WSMsgType

LIVE_MEETING_URL = "http://server.natsoft.com.au:8080/LiveMeeting/20260419.CPR"
LOG_DIR = pathlib.Path("natsoft_logs")
STATE_DIR = pathlib.Path("natsoft_state")
STATE_FILE = STATE_DIR / "live_state.json"


def meeting_url_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    if url.startswith("ws://") or url.startswith("wss://"):
        return url
    raise ValueError(f"Unsupported URL format: {url}")


def decode_natsoft_message(data: str) -> str:
    if not data:
        return data

    if data[0] == "<":
        return data

    ln = 0x7F
    out: list[str] = []

    for ch in data:
        decoded = ord(ch) ^ ln

        if decoded > 31:
            out.append(chr(decoded))
        elif decoded == 22:
            out.append("><")
        elif decoded == 23:
            out.append(' T="')
        elif decoded == 24:
            out.append('="0.0000" ')
        elif decoded == 25:
            out.append('="0" ')
        elif decoded == 26:
            out.append('="" ')
        elif decoded == 27:
            out.append('" /><')
        elif decoded == 28:
            out.append('"><')
        elif decoded == 29:
            out.append('" />')
        elif decoded == 30:
            out.append('="')
        elif decoded == 31:
            out.append('" ')
        else:
            out.append(chr(decoded))

        ln = decoded

    return "".join(out)


def make_log_path() -> pathlib.Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return LOG_DIR / f"natsoft_phase3_{stamp}.log"


def safe_int(value: Optional[str], default: int = 0) -> int:
    if value is None or value == "" or value == "?":
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def safe_float(value: Optional[str], default: float = 0.0) -> float:
    if value is None or value == "" or value == "?":
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass
class Competitor:
    result_id: int
    car_number: str = ""
    display_number: str = ""
    vehicle: str = ""
    driver_name: str = ""


@dataclass
class Standing:
    position: int
    display_position: str = ""
    result_ref: int = -1
    laps: int = 0
    last_lap: float = 0.0
    best_lap: float = 0.0
    gap_laps: str = ""
    gap_time: str = ""
    pit_flag: str = ""
    track_segment: str = ""


@dataclass
class RaceState:
    meeting_name: str = ""
    event_code: str = ""
    event_name: str = ""
    event_type: str = ""
    session_status: str = ""
    countdown_laps: int = 0
    elapsed_value: int = 0
    track_code: str = ""
    track_name: str = ""
    competitors: Dict[int, Competitor] = field(default_factory=dict)
    standings: Dict[int, Standing] = field(default_factory=dict)
    updated_at: str = ""

    def update_from_xml(self, xml_text: str) -> None:
        root = ET.fromstring(xml_text)

        if root.tag in {"New", "Change", "WaitCat"}:
            for child in root:
                self._import_node(child)
        else:
            self._import_node(root)

        self.updated_at = datetime.now(timezone.utc).isoformat()

    def _import_node(self, node: ET.Element) -> None:
        tag = node.tag

        if tag == "M":
            self.meeting_name = node.attrib.get("D", self.meeting_name)

        elif tag == "T":
            self.track_code = node.attrib.get("C", self.track_code)
            self.track_name = node.attrib.get("N", self.track_name)

        elif tag == "E":
            self.event_code = node.attrib.get("C", self.event_code)
            self.event_name = node.attrib.get("D", self.event_name)
            self.event_type = node.attrib.get("Y", self.event_type)

        elif tag == "S":
            self.session_status = node.attrib.get("S", self.session_status)

        elif tag == "C":
            self.countdown_laps = safe_int(node.attrib.get("C"), self.countdown_laps)
            self.elapsed_value = safe_int(node.attrib.get("E"), self.elapsed_value)

        elif tag == "RL":
            self._parse_rl(node)

        elif tag == "L":
            self._parse_l(node)

    def _parse_rl(self, rl_node: ET.Element) -> None:
        for r in rl_node.findall("R"):
            result_id = safe_int(r.attrib.get("ID"), -1)
            if result_id < 0:
                continue

            comp = self.competitors.get(result_id)
            if comp is None:
                comp = Competitor(result_id=result_id)
                self.competitors[result_id] = comp

            comp.car_number = r.attrib.get("C", comp.car_number)
            comp.display_number = r.attrib.get("N", comp.display_number)
            comp.vehicle = r.attrib.get("V", comp.vehicle)

            first_driver = r.find("V")
            if first_driver is not None:
                comp.driver_name = first_driver.attrib.get("N", comp.driver_name).replace("_", " ")

    def _parse_l(self, l_node: ET.Element) -> None:
        update_mode = l_node.attrib.get("Y", "")

        if update_mode.startswith("f"):
            self.standings.clear()

        for p in l_node.findall("P"):
            position = safe_int(p.attrib.get("L"), 0)
            if position <= 0:
                continue

            standing = self.standings.get(position)
            if standing is None:
                standing = Standing(position=position)

            standing.display_position = p.attrib.get("LP", "") or standing.display_position or str(position)
            standing.result_ref = safe_int(p.attrib.get("C"), standing.result_ref)

            d = p.find("D")
            if d is not None:
                if "L" in d.attrib:
                    standing.laps = safe_int(d.attrib.get("L"), standing.laps)

                if "I" in d.attrib:
                    standing.last_lap = safe_float(d.attrib.get("I"), standing.last_lap)

                if "FI" in d.attrib:
                    new_best = safe_float(d.attrib.get("FI"), standing.best_lap)
                    if new_best > 0:
                        standing.best_lap = new_best

                if "GL" in d.attrib:
                    standing.gap_laps = d.attrib.get("GL", standing.gap_laps)

                if "GI" in d.attrib:
                    standing.gap_time = d.attrib.get("GI", standing.gap_time)

                if "P" in d.attrib:
                    standing.pit_flag = d.attrib.get("P", standing.pit_flag)

                if "LL" in d.attrib:
                    standing.track_segment = d.attrib.get("LL", standing.track_segment)

            self.standings[position] = standing

    def to_json_dict(self) -> dict:
        standings_out = []

        for pos in sorted(self.standings):
            s = self.standings[pos]

            if s.result_ref < 0 and s.laps == 0 and s.last_lap == 0.0 and s.best_lap == 0.0:
                continue

            comp = self.competitors.get(s.result_ref)

            standings_out.append(
                {
                    "position": s.position,
                    "display_position": s.display_position,
                    "result_ref": s.result_ref,
                    "car": comp.display_number if comp and comp.display_number else (comp.car_number if comp else ""),
                    "car_number_raw": comp.car_number if comp else "",
                    "driver": comp.driver_name if comp else "",
                    "vehicle": comp.vehicle if comp else "",
                    "laps": s.laps,
                    "last_lap": s.last_lap,
                    "best_lap": s.best_lap,
                    "gap_time": s.gap_time,
                    "gap_laps": s.gap_laps,
                    "pit_flag": s.pit_flag,
                    "track_segment": s.track_segment,
                }
            )

        return {
            "updated_at": self.updated_at,
            "meeting_name": self.meeting_name,
            "track_code": self.track_code,
            "track_name": self.track_name,
            "event_code": self.event_code,
            "event_name": self.event_name,
            "event_type": self.event_type,
            "session_status": self.session_status,
            "countdown_laps": self.countdown_laps,
            "elapsed_value": self.elapsed_value,
            "standings": standings_out,
        }

    def render_leaderboard(self) -> str:
        lines: list[str] = []

        lines.append("")
        lines.append("=" * 100)
        lines.append(f"Meeting: {self.meeting_name}")
        lines.append(f"Track:   {self.track_name} ({self.track_code})")
        lines.append(f"Event:   {self.event_name}")
        lines.append(f"Type:    {self.event_type}")
        lines.append(f"Status:  {self.session_status}")
        lines.append("")

        header = f"{'Pos':<4} {'Car':<5} {'Driver':<24} {'Laps':<5} {'Last Lap':<10} {'Best Lap':<10} {'Gap':<10}"
        lines.append(header)
        lines.append("-" * len(header))

        displayed_rows = 0

        for row in self.to_json_dict()["standings"]:
            driver = row["driver"][:24]
            gap = row["gap_time"] or row["gap_laps"] or ""
            lines.append(
                f"{row['position']:<4} {row['car']:<5} {driver:<24} {row['laps']:<5} {row['last_lap']:<10.4f} {row['best_lap']:<10.4f} {gap:<10}"
            )
            displayed_rows += 1

        if displayed_rows == 0:
            lines.append("(no standings yet)")

        lines.append("=" * 100)
        lines.append("")
        return "\n".join(lines)


APP_STATE: dict = {
    "race_state": None,
}


def write_state_file(state: RaceState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = state.to_json_dict()
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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
    setInterval(loadLive, 1000);
  </script>
</body>
</html>
"""


async def handle_root(request: web.Request) -> web.Response:
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_dashboard(request: web.Request) -> web.Response:
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_live(request: web.Request) -> web.Response:
    state = APP_STATE["race_state"]
    if state is None:
        return web.json_response({"status": "starting", "standings": []})
    return web.json_response(state.to_json_dict())


async def start_api() -> None:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/live", handle_live)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Dashboard running on port {port}")
    print(f"Local dashboard: http://127.0.0.1:{port}/")
    print(f"Live JSON: http://127.0.0.1:{port}/live")

    while True:
        await asyncio.sleep(3600)


async def run_collector(meeting_url: str) -> None:
    ws_url = meeting_url_to_ws(meeting_url)
    log_path = make_log_path()
    state = RaceState()
    APP_STATE["race_state"] = state
    last_render = ""

    print(f"Connecting to {ws_url}")
    print(f"Logging to {log_path}")

    async with ClientSession() as session:
        async with session.ws_connect(
            ws_url,
            heartbeat=30,
            autoclose=True,
            autoping=True,
        ) as websocket:
            print("Connected.\n")

            with log_path.open("a", encoding="utf-8") as log_file:
                async for message in websocket:
                    if message.type == WSMsgType.TEXT:
                        raw_text = message.data
                    elif message.type == WSMsgType.BINARY:
                        raw_text = message.data.decode("utf-8", errors="replace")
                    elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        raise RuntimeError("WebSocket connection closed")
                    else:
                        continue

                    decoded = decode_natsoft_message(raw_text)

                    timestamp = datetime.now(timezone.utc).isoformat()
                    log_file.write(f"\n[{timestamp}]\n{decoded}\n")
                    log_file.flush()

                    if not decoded.startswith("<"):
                        continue

                    try:
                        state.update_from_xml(decoded)
                    except ET.ParseError:
                        continue

                    write_state_file(state)

                    if (
                        decoded.startswith("<L")
                        or decoded.startswith("<New")
                        or decoded.startswith("<S")
                        or decoded.startswith("<C")
                    ):
                        rendered = state.render_leaderboard()
                        if rendered != last_render:
                            print("\033[2J\033[H", end="")
                            print(rendered)
                            last_render = rendered


async def collector_loop() -> None:
    while True:
        try:
            await run_collector(LIVE_MEETING_URL)
        except Exception as exc:
            print(f"\nConnection failed: {exc}")
            print("Retrying in 3 seconds...\n")
            await asyncio.sleep(3)


async def main() -> None:
    await asyncio.gather(
        start_api(),
        collector_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())