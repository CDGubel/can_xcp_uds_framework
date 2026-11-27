# ECU Communication and Calibration Framework

An ECU-independent toolkit for bench work on classical CAN and CAN-FD, with a
command-line interface and a hardware-free test suite. It has two backends:
in-process virtual CAN and PEAK PCAN-Basic.

- **Milestone 1:** raw CAN and CAN-FD frames (`ecu-can send`, `listen`, `demo`).
- **Milestone 2:** ISO-TP (ISO 15765-2) with `can-isotp` and UDS (ISO 14229)
  with `udsoncan` (`ecu-can uds ...`), ECU profiles in TOML, and a simulated
  ECU for development without hardware.
- **Bench UDS:** functional addressing, SecurityAccess with pluggable key
  algorithms, RoutineControl, WriteDataByIdentifier, a `uds shell` that runs
  scripts over one connection with TesterPresent in the background, and a
  simulated "test_board" with checks that can also run against the real board.

## Set up on Windows 11

From PowerShell, in the project folder (for example a clone of this repository):

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ecu-can.exe demo
.\.venv\Scripts\ecu-can.exe demo --fd
.\.venv\Scripts\ecu-can.exe uds demo
```

In VS Code, open the project folder and select `.venv\Scripts\python.exe` with
**Python: Select Interpreter**.

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

## UDS diagnostics over ISO-TP

`ecu-can uds` sends UDS requests with normal ISO-TP addressing: the tester
sends on `--tx-id` and the ECU answers on `--rx-id`. All bus options above
apply. Try it first against the built-in simulated ECU:

```powershell
.\.venv\Scripts\ecu-can.exe uds demo
```

Commands against a real ECU (here 500 kbit/s, 11-bit IDs 0x7E0/0x7E8):

```powershell
$ecu = "--interface", "pcan", "--tx-id", "0x7E0", "--rx-id", "0x7E8"
.\.venv\Scripts\ecu-can.exe uds session extended @ecu
.\.venv\Scripts\ecu-can.exe uds tester-present @ecu
.\.venv\Scripts\ecu-can.exe uds read-did 0xF190 0xF187 @ecu
.\.venv\Scripts\ecu-can.exe uds reset hard @ecu
.\.venv\Scripts\ecu-can.exe uds raw "22 F1 90" @ecu
.\.venv\Scripts\ecu-can.exe uds write-did 0xF184 "41 42 43" @ecu
.\.venv\Scripts\ecu-can.exe uds routine start 0xFF00 "01 02" @ecu
.\.venv\Scripts\ecu-can.exe uds functional "3E 80" --functional-id 0x7DF @ecu
.\.venv\Scripts\ecu-can.exe uds unlock 1 --key-file C:\keys\my_ecu.key @ecu
```

- `session` accepts `default`, `programming`, `extended` or a number, and
  `reset` accepts `hard`, `key-off-on`, `soft` or a number.
- `read-did` sends one request per DID and prints each value as hex, plus
  ASCII text when every byte is printable.
- `raw` sends the bytes exactly as given and prints the response, including
  negative responses. It waits through NRC 0x78 (response pending). With
  `--expect HEX` it succeeds only when the response starts with those bytes
  (a negative response can be the expected one); `--expect none` expects no
  response.
- `routine` takes `start`, `stop`, `result` or a number, the RID and an
  optional option record, and prints the routine status record.
- `functional` sends one single frame to `--functional-id` (7 bytes on
  classic CAN) and lists every response on `--rx-id` within `--listen` seconds
  (default 0.5). `--expect` works as for `raw`.
- `unlock LEVEL` requests a seed, computes the key and sends it. An all-zero
  seed means the level is already unlocked. See *SecurityAccess keys* below.
- Commands exit with status 1 on a negative response, a timeout or an error,
  and the message names the negative response code.
- ISO-TP options: `--extended` for 29-bit IDs, `--frame-fd` (with `--fd`) for
  FD frames of up to 64 bytes, `--brs`, `--padding` (default `0xCC`, or
  `none`), `--stmin`, `--blocksize`, and `--timeout` in seconds (default 2).
- Add `-v` before the command (`ecu-can -v uds ...`) to see the ISO-TP and
  UDS library logs.
- Reset, session and raw requests change ECU state. Use them on a bench ECU.

### ECU profiles

Put ECU-specific settings in a TOML profile instead of repeating options.
Start from [profiles/example_ecu.toml](profiles/example_ecu.toml), which lists
every key. Options given on the command line override the profile, and every
command (`send`, `listen` and `uds`) accepts `--ecu`:

```powershell
.\.venv\Scripts\ecu-can.exe uds read-did 0xF190 --ecu profiles\my_ecu.toml
```

### SecurityAccess keys

Key algorithms are pluggable: `--key-algorithm aes128-ecb` (the default:
key = AES-128-ECB(secret, seed)) or `package.module:function` for your own
`function(seed, level, secret) -> key`. The secret is read at run time from
`--key-file` (hex text), `security.key_file` in the profile, or the
`ECU_COMM_SECURITY_KEY` environment variable. **Never commit secrets:**
`*.key` files and `keys/` are ignored by git; keep them outside the repository.
AES needs the `cryptography` package (`pip install -e ".[security]"`, included
in `[dev]`).

```powershell
$env:ECU_COMM_SECURITY_KEY = Get-Content C:\keys\my_ecu.key
```

### Scripts and the UDS shell

Each command above opens and closes its own connection, so an ECU can fall
back to the default session between them. `uds shell` keeps one connection
open and sends TesterPresent (`3E 80`, response suppressed) every
`--keep-alive` seconds (default 2, `uds.keep_alive` in a profile, 0 disables
it). It reads one command per line, the same commands as above without the bus
options, plus `sleep SECONDS`, `help` and `quit`; `#` starts a comment.

```powershell
.\.venv\Scripts\ecu-can.exe uds shell --ecu profiles\test_board.toml                 # type commands
.\.venv\Scripts\ecu-can.exe uds shell --ecu profiles\test_board.toml my_steps.uds    # run a script
```

A script (or piped input) echoes each line and stops at the first failure with
exit status 1; typed commands keep going after an error.

### The test_board

[profiles/test_board.toml](profiles/test_board.toml) describes a bench board:
PT-CAN on `PCAN_USBBUS2`, classic CAN at 500 kbit/s, requests on 0x60A,
responses on 0x68A, functional requests on 0x601, padding 0xAA.
[scripts/test_board_readonly.uds](scripts/test_board_readonly.uds) checks it
without writing, unlocking or running routines:

```powershell
.\.venv\Scripts\ecu-can.exe uds shell --ecu profiles\test_board.toml scripts\test_board_readonly.uds
```

`uds simulate --board test_board` imitates the board (sessions and timing, S3,
DIDs, SecurityAccess with attempt limit and delay, the sensor-transaction
routine 0xFD00 and its preconditions, functional addressing). It uses a public
throw-away secret unless a key file or `ECU_COMM_SECURITY_KEY` is given.

`tests/test_board_checks.py` runs against that simulator by default. With
`--board-profile` it runs the hardware-safe checks against the real board;
checks marked `sim_only` (writes, programming session, communication control)
are skipped there:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_board_checks.py --board-profile profiles\test_board.toml
```

### Simulated ECU

`ecu-can uds simulate` answers DiagnosticSessionControl (0x10), ECUReset
(0x11), ReadDataByIdentifier (0x22: F187, F18C, F190, F195) and TesterPresent
(0x3E). It answers every other service with NRC 0x11. Over PCAN you can run it
on a second adapter or channel of the same bus and point the tester at it:

```powershell
.\.venv\Scripts\ecu-can.exe uds simulate --interface pcan --channel PCAN_USBBUS2 --tx-id 0x7E0 --rx-id 0x7E8
```

Use the same `--tx-id` and `--rx-id` as the tester. The simulator swaps them
itself. `--functional-id` makes it answer functional requests too, and
`--board test_board` selects the test_board behaviour. On the virtual backend it only reaches testers in the same process, as
the tests and `uds demo` do.

## Project layout and next milestones

- `src/ecu_comm/transport.py` opens backends, configures bit timing, and
  creates and sends validated `python-can` messages.
- `src/ecu_comm/isotp_link.py` runs an ISO-TP stack on a bus (`IsoTpConfig`,
  `open_isotp`).
- `src/ecu_comm/uds.py` opens a `udsoncan` client (`open_uds_client`) and adds
  raw requests, raw DID reads, functional requests and `unlock`.
- `src/ecu_comm/security.py` holds the key algorithms and loads secrets.
- `src/ecu_comm/sim_ecu.py` is the simulated UDS server, and
  `sim_test_board.py` the simulated test_board.
- `src/ecu_comm/ecu_profile.py` loads and checks TOML profiles.
- `src/ecu_comm/cli.py` and `uds_cli.py` provide the `ecu-can` commands.
- `tests/` covers raw frames, ISO-TP (single and multi-frame, padding, FD,
  29-bit), UDS against the simulated ECUs, security keys, the CLI, the shell
  and the profiles.

From Python, `open_uds_client` returns a standard `udsoncan` client, so all
its services are available, including DIDs with your own codecs
(`data_identifiers=`).

The remaining planned integrations are DBC decoding with `cantools` and XCP
with `pyXCP`. A2L and DBC files will belong in external configuration alongside
the ECU profiles.
