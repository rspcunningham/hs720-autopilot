"""
Flight state dataclass and telemetry parser.
"""

import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum

from .protocol import START_BYTE, MsgId


class FlightMode(IntEnum):
    LOCKED_NO_GPS = 0
    ALTITUDE_HOLD = 1
    POSITION_HOLD = 2
    RETURN_HOME   = 3
    FOLLOW_ME     = 4
    CIRCLE        = 5
    WAYPOINT      = 6
    SHUTDOWN      = 7
    GYRO_CAL      = 8
    COMPASS_H_CAL = 9
    COMPASS_V_CAL = 10
    LANDING       = 11
    IDLE          = 12
    LOCKED_GPS    = 13
    FLY_FORWARD   = 14
    FLY_BACKWARD  = 15
    DISTANCE_FLY  = 16
    POINT_CIRCLE  = 17


_FLYING_MODES = {
    FlightMode.ALTITUDE_HOLD, FlightMode.POSITION_HOLD,
    FlightMode.RETURN_HOME, FlightMode.FOLLOW_ME,
    FlightMode.CIRCLE, FlightMode.WAYPOINT,
    FlightMode.LANDING, FlightMode.FLY_FORWARD,
    FlightMode.FLY_BACKWARD, FlightMode.DISTANCE_FLY,
    FlightMode.POINT_CIRCLE,
}


@dataclass
class FlightState:
    mode: int = 0
    lat: float = 0.0
    lon: float = 0.0
    altitude: int = 0
    distance: int = 0
    voltage: float = 0.0
    satellites: int = 0
    speed: float = 0.0
    yaw: int = 0
    return_alt: int = 0       # RTH hover altitude (m)
    fence_alt: int = 0        # geofence max altitude (m)
    fence_dist: int = 0       # geofence radius (m)
    circle_radius: int = 0    # orbit mode radius
    status1: int = 0          # bitfield: errors + state flags
    signal: int = 0           # bitfield: ctrl source + signal strength
    status2: int = 0          # bitfield: sensors + modes
    return_point: bool = False # home point recorded
    timestamp: float = field(default_factory=time.time)

    @property
    def mode_name(self) -> str:
        try:
            return FlightMode(self.mode).name.replace("_", " ").title()
        except ValueError:
            return f"Unknown({self.mode})"

    @property
    def flying(self) -> bool:
        try:
            return FlightMode(self.mode) in _FLYING_MODES
        except ValueError:
            return False

    @property
    def signal_strength(self) -> int:
        return (self.signal >> 4) & 0x07

    @property
    def app_control(self) -> bool:
        return bool(self.signal & 0x08)

    @property
    def low_battery(self) -> bool:
        return bool(self.status1 & 0x01)

    @property
    def critical_battery(self) -> bool:
        return bool(self.status1 & 0x02)

    @property
    def initialized(self) -> bool:
        return bool(self.status1 & 0x08)

    @property
    def gyro_error(self) -> bool:
        return bool(self.status1 & 0x10)

    @property
    def baro_error(self) -> bool:
        return bool(self.status1 & 0x20)

    @property
    def compass_error(self) -> bool:
        return bool(self.status1 & 0x40)

    @property
    def gps_error(self) -> bool:
        return bool(self.status1 & 0x80)

    @property
    def headless(self) -> bool:
        return bool(self.status2 & 0x20)

    @property
    def recording(self) -> bool:
        return (self.status2 >> 6) & 0x01 == 1


def parse_telemetry(data: bytes) -> FlightState | None:
    """Parse a 0xAA GPSInFo packet into FlightState. Returns None if not valid."""
    if len(data) < 4 or data[0] != START_BYTE or data[1] != MsgId.GPSInFo:
        return None

    state = FlightState(timestamp=time.time())

    if len(data) >= 8:
        state.lon = struct.unpack_from("<i", data, 4)[0] / 1e7
    if len(data) >= 12:
        state.lat = struct.unpack_from("<i", data, 8)[0] / 1e7
    if len(data) >= 14:
        state.altitude = struct.unpack_from("<h", data, 12)[0]
    if len(data) >= 16:
        state.distance = struct.unpack_from("<h", data, 14)[0]
    if len(data) >= 17:
        state.return_alt = data[16]
    if len(data) >= 18:
        state.fence_alt = data[17]
    if len(data) >= 20:
        state.fence_dist = struct.unpack_from("<h", data, 18)[0]
    if len(data) >= 21:
        state.circle_radius = data[20]
    if len(data) >= 22:
        state.mode = data[21]
    if len(data) >= 23:
        state.voltage = data[22] / 10.0
    if len(data) >= 24:
        state.satellites = data[23] & 0x1F
        state.return_point = bool(data[23] & 0x40)
    if len(data) >= 25:
        state.status1 = data[24]
    if len(data) >= 26:
        state.signal = data[25]
    if len(data) >= 27:
        state.speed = data[26] / 10.0
    if len(data) >= 28:
        state.status2 = data[27]
    if len(data) >= 30:
        state.yaw = struct.unpack_from("<h", data, 28)[0]

    return state
