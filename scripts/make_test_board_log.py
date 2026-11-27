"""Write examples/test_board_sample.asc: one second of invented test_board traffic for trying `ecu-can decode`.

    python scripts/make_test_board_log.py

The frames are built from profiles/test_board.dbc (an invented network), so the log describes no real product.
"""

from pathlib import Path

import can
import cantools

ROOT = Path(__file__).parents[1]
DBC = ROOT / "profiles" / "test_board.dbc"
OUTPUT = ROOT / "examples" / "test_board_sample.asc"


def frames() -> list[can.Message]:
    db = cantools.database.load_file(DBC)
    status, currents = db.get_message_by_name("BoardStatus"), db.get_message_by_name("PhaseCurrents")
    info, command = db.get_message_by_name("BoardInfo"), db.get_message_by_name("TesterCommand")

    def frame(time, message, values, **flags):
        return can.Message(timestamp=time, arbitration_id=message.frame_id, is_extended_id=message.is_extended_frame,
                           is_fd=message.is_fd, data=message.encode(values), **flags)

    out = [frame(0.0, info, {"Page": 0, "SerialNumber": 4200017}),
           frame(0.5, info, {"Page": 1, "SwMajor": 2, "SwMinor": 7}),
           can.Message(timestamp=0.62, arbitration_id=0x7FF, is_extended_id=False, data=bytes.fromhex("01 02")),
           frame(0.75, command, {"Mode": "Speed", "SpeedRequest": 1500, "TorqueLimit": 40}, bitrate_switch=True)]
    for step in range(10):
        time = step / 10
        running = step >= 3
        out.append(frame(time + 0.001, status, {
            "State": "Running" if running else "Ready", "Counter": step, "SupplyVoltage": 24.12 - 0.01 * (step % 3),
            "BoardTemperature": 31 + step // 4, "FaultActive": 0}))
        amps = (12.0 + 0.1 * step) if running else 0.0
        out.append(frame(time + 0.004, currents, {"CurrentU": amps, "CurrentV": -amps / 2, "CurrentW": -amps / 2}))
    return sorted(out, key=lambda m: m.timestamp)


def main() -> None:
    OUTPUT.parent.mkdir(exist_ok=True)
    with can.ASCWriter(OUTPUT) as writer:
        for message in frames():
            writer.on_message_received(message)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
