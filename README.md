# ECU Communication and Calibration Framework

Milestone 1 provides an ECU-independent classical CAN and CAN-FD transport with
`python-can`, a command-line interface, and a hardware-free test suite. It has
two backends: in-process virtual CAN and PEAK PCAN-Basic. The CLI sends raw
data frames, displays received frames, and includes a virtual round-trip demo.

## Set up on Windows 11

Download and extract the ZIP to `C:\can_xcp_uds_framework`. The ZIP contains the
project files at its root, so select that directory as the extraction target.
If the directory already contains your work, extract to a new directory first.

From PowerShell:

```powershell
Set-Location C:\can_xcp_uds_framework
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ecu-can.exe demo
.\.venv\Scripts\ecu-can.exe demo --fd
```

In VS Code, open the project folder and select `.venv\Scripts\python.exe` with
**Python: Select Interpreter**. The project is ready for an optional local Git
repository. Run `git init` in the extracted folder if you want to start one.

You can also run the CLI as `python -m ecu_comm` with the project's interpreter.

## Use the PEAK PCAN-USB FD on a bench bus

The PCAN backend requires PEAK's Windows driver and PCAN-Basic runtime
matching your Python architecture. Use the bus settings for your bench.
The channel below assumes the first PCAN USB channel. Use `ecu-can listen --help`
or `ecu-can send --help` for the available options.

Examples for a classical bus at 500 kbit/s and a CAN-FD bus at 500 kbit/s
nominal, 2 Mbit/s data. The transmit examples use an arbitrary ID and
payload intended for an isolated test bus.

```powershell
.\.venv\Scripts\ecu-can.exe listen --interface pcan --channel PCAN_USBBUS1 --bitrate 500000 --duration 10
.\.venv\Scripts\ecu-can.exe send --interface pcan --channel PCAN_USBBUS1 --bitrate 500000 0x123 "01 02 AB"
.\.venv\Scripts\ecu-can.exe listen --interface pcan --channel PCAN_USBBUS1 --fd --bitrate 500000 --data-bitrate 2000000 --duration 10
.\.venv\Scripts\ecu-can.exe send --interface pcan --channel PCAN_USBBUS1 --fd --bitrate 500000 --data-bitrate 2000000 --frame-fd --brs 0x123 "00 01 02 03 04 05 06 07 08 09 0A 0B"
```

- `--fd` opens the adapter in FD mode. Add `--frame-fd` to send an FD frame,
  and add `--brs` when the frame should switch to the data bitrate. An FD-mode
  bus also supports sending classical frames.
- IDs are 11-bit by default. Add `--extended` for 29-bit IDs. IDs accept
  decimal and `0x`-prefixed hexadecimal.
- Payload lengths are 0–8 bytes for classical CAN and 0–8, 12, 16, 20, 24, 32,
  48, or 64 bytes for CAN-FD. The CLI rejects other lengths to preserve the
  payload you provide. Brackets in CLI output show the length in bytes.
- The PCAN FD configuration uses an 80 MHz controller clock and targets
  80% nominal and 75% data sample points by default. Use
  `--nominal-sample-point` and `--data-sample-point` to request other sample
  points. Achievable sample points depend on bitrate and controller quanta.
- `listen` sends no application frames. PCAN opens in normal bus mode, so
  its controller can acknowledge traffic. Use a second active CAN node on
  a properly terminated bench bus to check physical transmission.
- The default virtual backend works within one Python process. Use `demo`
  for a standalone round trip. Separate CLI processes cannot exchange frames
  over this backend. Virtual tests exercise frame handling and CLI behavior.
  Physical bit timing, bus arbitration, and acknowledgement need bench validation.

Reference: [python-can PCAN backend](https://python-can.readthedocs.io/en/stable/interfaces/pcan.html)
and [python-can VirtualBus](https://python-can.readthedocs.io/en/stable/interfaces/virtual.html).

## Project layout and next milestones

- `src/ecu_comm/transport.py` opens backends, configures bit timing, and
  creates and sends validated `python-can` messages.
- `src/ecu_comm/cli.py` provides the `send`, `listen`, and `demo` commands.
- `tests/` exercises raw frame exchange, the CLI, validation, and the
  parameters passed to the PCAN backend.

The transport returns a standard `python-can` bus so future protocol modules
can use it directly. The planned integrations are DBC decoding with
`cantools`, ISO-TP with `can-isotp`, UDS with `udsoncan`, and XCP with
`pyXCP`. Only the Milestone 1 dependencies are installed by this release.
ECU-specific settings such as CAN IDs and A2L or DBC files will belong in
external configuration for those integrations.
