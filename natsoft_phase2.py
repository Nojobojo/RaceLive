import asyncio
import pathlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

import websockets

LIVE_MEETING_URL = "http://server.natsoft.com.au:8080/LiveMeeting/20260419.CPR"
LOG_DIR = pathlib.Path("natsoft_logs")


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
    return LOG_DIR / f"natsoft_phase2_{stamp}.log"


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

    def update_from_xml(self, xml_text: str) -> None:
        root = ET.fromstring(xml_text)

        if root.tag in {"New", "Change", "WaitCat"}:
            for child in root:
                self._import_node(child)
        else:
            self._import_node(root)

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

        for pos in sorted(self.standings):
            s = self.standings[pos]

            if s.result_ref < 0 and s.laps == 0 and s.last_lap == 0.0 and s.best_lap == 0.0:
                continue

            comp = self.competitors.get(s.result_ref)

            car = comp.display_number if comp and comp.display_number else (comp.car_number if comp else "")
            driver = comp.driver_name if comp and comp.driver_name else f"Ref {s.result_ref}"

            if len(driver) > 24:
                driver = driver[:24]

            gap = s.gap_time or s.gap_laps or ""
            lines.append(
                f"{pos:<4} {car:<5} {driver:<24} {s.laps:<5} {s.last_lap:<10.4f} {s.best_lap:<10.4f} {gap:<10}"
            )
            displayed_rows += 1

        if displayed_rows == 0:
            lines.append("(no standings yet)")

        lines.append("=" * 100)
        lines.append("")
        return "\n".join(lines)


async def run_collector(meeting_url: str) -> None:
    ws_url = meeting_url_to_ws(meeting_url)
    log_path = make_log_path()
    state = RaceState()
    last_render = ""

    print(f"Connecting to {ws_url}")
    print(f"Logging to {log_path}")

    async with websockets.connect(
        ws_url,
        ping_interval=None,
        ping_timeout=None,
        max_size=None,
    ) as websocket:
        print("Connected.\n")

        with log_path.open("a", encoding="utf-8") as log_file:
            while True:
                message = await websocket.recv()

                if isinstance(message, bytes):
                    raw_text = message.decode("utf-8", errors="replace")
                else:
                    raw_text = message

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

                if decoded.startswith("<L") or decoded.startswith("<New") or decoded.startswith("<S") or decoded.startswith("<C"):
                    rendered = state.render_leaderboard()
                    if rendered != last_render:
                        print("\033[2J\033[H", end="")
                        print(rendered)
                        last_render = rendered


async def main() -> None:
    while True:
        try:
            await run_collector(LIVE_MEETING_URL)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            return
        except Exception as exc:
            print(f"\nConnection failed: {exc}")
            print("Retrying in 3 seconds...\n")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())