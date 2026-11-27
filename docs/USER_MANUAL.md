# ecu-can User's Manual

`ecu-can` is a command-line tool for bench work on CAN and CAN-FD: raw frames,
UDS diagnostics over ISO-TP, scripted test sequences, decoding and encoding with
DBC files, recorded logs, and a simulated ECU for work without hardware. This manual takes you from installation to running your
own scripts against a board, with real commands and the output they produce.

**How to read the examples.** Every example is a PowerShell command followed by
its output, exactly as printed. Lines starting with `ecu-can:` go to the error
stream. Each example is labelled with where its output came from:

- **Output (no hardware)**: produced on a PC with no adapter connected.
- **Output (B19)**: produced on the bench board B19 through a PEAK PCAN-USB FD.
- **Output (simulator, B19 pending)**: produced by the simulated test_board,
  which imitates B19. It is replaced with B19 output after the bench run. Values
  such as serial numbers and versions differ on a real board; the format does not.

Some B19 output was captured by the shell (section 6), which prints a response in
exactly the same form as the single command.

## Contents

1. [Install and check](#1-install-and-check)
2. [Five things to know first](#2-five-things-to-know-first)
3. [ECU profiles](#3-ecu-profiles)
4. [First session with a board](#4-first-session-with-a-board)
5. [UDS command reference](#5-uds-command-reference)
6. [Scripts and the UDS shell](#6-scripts-and-the-uds-shell)
7. [SecurityAccess keys](#7-securityaccess-keys)
8. [Working without hardware](#8-working-without-hardware)
9. [Raw CAN frames](#9-raw-can-frames)
10. [DBC files: signals instead of bytes](#10-dbc-files-signals-instead-of-bytes)
11. [Troubleshooting](#11-troubleshooting)
12. [Reference tables](#12-reference-tables)

---

## 1. Install and check

You need Windows 11, Python 3.13, and for hardware the PEAK driver with the
PCAN-Basic runtime. In PowerShell, in the project folder:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Activate the environment so `ecu-can` works without its full path. Do this once
per PowerShell window:

```powershell
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses to run the script, allow local scripts for your user once
with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or skip activation
and type `.\.venv\Scripts\ecu-can.exe` wherever this manual says `ecu-can`.

Check the installation. These commands need no hardware.

```powershell
ecu-can demo
```

**Output (no hardware)**

```text
Virtual round trip OK. 0x123 STD CAN [8] 00 01 02 03 04 05 06 07
```

```powershell
ecu-can uds demo
```

**Output (no hardware)**

```text
Session 0x03 (extended) active.
Tester present OK
0xF190 [17] 57 53 49 4D 55 4C 41 54 45 44 30 30 30 30 30 30 31 "WSIMULATED0000001"
0xF187 [13] 53 49 4D 2D 50 41 52 54 2D 30 30 30 31 "SIM-PART-0001"
Raw 27 01 -> 7F 27 11 (negative response 0x11 (ServiceNotSupported))
UDS demo OK.
```

The last request is refused on purpose: the demo ECU does not support
SecurityAccess, which shows what a negative response looks like.

Every command has built-in help: `ecu-can --help`, `ecu-can uds --help`,
`ecu-can uds read-did --help`.

---

## 2. Five things to know first

1. **Numbers.** IDs, DIDs and levels accept decimal or `0x` hex:
   `0xF190` and `61840` are the same DID.
2. **Byte strings** are hex pairs in quotes: `"22 F1 90"`. Spaces are optional
   (`"22F190"` works) but make output easier to compare.
3. **Output format.** `[n]` is a length in bytes. DID values print as hex.
   When the profile names the DID (section 3), its name comes before the length
   and its fields after the bytes; otherwise the text follows in quotes when
   every byte is printable.
4. **Exit status.** Each command exits with 0 on success and 1 on a negative
   response, a timeout or an error, so scripts and CI can test it. In
   PowerShell, read it with `$LASTEXITCODE`.
5. **Each command is one connection.** `ecu-can uds session extended` opens the
   adapter, switches the session and closes again. The board returns to the
   default session when it hears nothing for its S3 time (5 s on B19). For
   anything that needs a session to persist, use the shell (section 6).

---

## 3. ECU profiles

A profile is a TOML file holding a board's bus and addressing settings, so you
don't repeat them on every command. `profiles\test_board.toml` describes B19:

```toml
[bus]
interface = "pcan"
channel = "PCAN_USBBUS2"    # the USB-CANFD adapter on PT-CAN
bitrate = 500000
fd = false

[isotp]
tx_id = 0x60A               # physical requests
rx_id = 0x68A               # responses (also to functional requests)
functional_id = 0x601       # functional requests, single frame only
padding = 0xAA
stmin = 0
blocksize = 0

[uds]
timeout = 2.0               # P2* of the board is 2 s
keep_alive = 2.0            # S3 is 5 s: send TesterPresent well inside it

[security]
algorithm = "aes128-ecb"
```

Use it with `--ecu`:

```powershell
ecu-can uds tester-present --ecu profiles\test_board.toml
```

- **Command line wins.** Any option you type overrides the profile, for example
  `--channel PCAN_USBBUS1` when the adapter enumerates differently.
- **New board:** copy `profiles\example_ecu.toml`, which lists every key with a
  comment, and edit it.
- **DBC files** for decoding go in a `[dbc]` section (section 10).
- **DID names and fields** go in a `[dids]` table (below).
- **Typos are caught.** A misspelt key stops the command before anything is
  sent:

  **Output (no hardware)**, for a profile with `txid` instead of `tx_id`:

  ```text
  ecu-can: C:\...\bad_profile.toml: unknown key isotp.txid
  ```

The rest of this manual uses a variable to keep commands short:

```powershell
$tb = "--ecu", "profiles\test_board.toml"
ecu-can uds tester-present @tb
```

`@tb` expands to the two words in the variable. Define it once per window.

### Naming DIDs

A `[dids]` table gives each DID a name and describes its fields, so `read-did`
shows values instead of bare bytes, and `read-did` and `write-did` accept the
name instead of the number. The test_board profile names 13 DIDs, for example:

```toml
[dids.0xFD10]
name = "PhaseCurrents"
fields = [
    { name = "U", type = "int16", scale = 0.1, unit = "A" },
    { name = "V", type = "int16", scale = 0.1, unit = "A" },
    { name = "W", type = "int16", scale = 0.1, unit = "A" },
]

[dids.0xF186]
name = "ActiveSession"
fields = [{ name = "Session", type = "uint8", choices = { 1 = "default", 2 = "programming", 3 = "extended" } }]
```

| Field key | Meaning |
|---|---|
| `name` | one word, unique within the DID |
| `type` | `uint8`, `uint16`, `uint24`, `uint32`, `int8`, `int16`, `int24`, `int32` (big-endian, as UDS sends them), `ascii` or `bytes` |
| `length` | bytes, for `ascii` and `bytes` only; the last field may leave it out to take the rest |
| `scale`, `offset` | physical value = raw x scale + offset (numbers only) |
| `unit` | printed after the value |
| `choices` | names for raw values, e.g. `{ 0 = "no", 1 = "yes" }` |

The fields are read in order, from the first byte. A DID with only a `name` is
still printed as bytes, under its name. Names are matched without regard to
case (`vin` finds `VIN`). Mistakes in the table stop the command before
anything is sent, for example `dids.0xF190: name must be one word`.

List the table without a bus:

```powershell
ecu-can uds dids @tb PhaseCurrents 0xF186
```

**Output (no hardware)**

```text
0xFD10 PhaseCurrents [6]
  U: int16, x0.1 A
  V: int16, x0.1 A
  W: int16, x0.1 A
0xF186 ActiveSession [1]
  Session: uint8, 1=default 2=programming 3=extended
```

Without names it lists every DID in the table.

---

## 4. First session with a board

This walk-through checks a board without changing anything on it: no writes, no
unlock, no routines. It assumes B19 is powered (24 V) and the PCAN-USB FD is on
PT-CAN.

### Step 1: is the board answering?

```powershell
ecu-can uds tester-present @tb
```

**Output (B19)**

```text
Tester present OK
```

### Step 2: which image is running, and which session?

```powershell
ecu-can uds read-did 0xF1A0 0xF186 @tb
```

**Output (B19)**

```text
0xF1A0 ActiveImage [1] 02  Image=application
0xF186 ActiveSession [1] 01  Session=default
```

The application is running (not the bootloader), in the default session. The
names and values come from the profile's `[dids]` table; the bytes are what the
board sent.

### Step 3: identification

```powershell
ecu-can uds read-did 0xF110 0xF111 0xF112 0xF189 0xF195 @tb
```

**Output (B19)**

```text
0xF110 CalibrationVersion [3] 01 00 00  Major=1, Minor=0, Patch=0
0xF111 BootloaderVersion [3] 00 00 00  Major=0, Minor=0, Patch=0
0xF112 HardwareVersion [3] 00 00 00  Major=0, Minor=0, Patch=0
0xF189 SoftwareVersion [3] 01 01 01  Major=1, Minor=1, Patch=1
0xF195 SupplierSoftwareVersion [3] 01 01 01  Major=1, Minor=1, Patch=1
```

These are the version DIDs, three bytes each. On B19 the fingerprints, serial
number and VIN (0xF183, 0xF184, 0xF18C, 0xF190) read as all zeros because they
have not been programmed, so their text is empty: `VIN=""`.

### Step 4: run the full read-only check

`scripts\test_board_readonly.uds` repeats the steps above and also checks
negative responses, the session timing and functional addressing. It stops at
the first unexpected answer.

```powershell
ecu-can uds shell @tb scripts\test_board_readonly.uds
```

**Output (B19)**

```text
> tester-present
Tester present OK
> raw '22 F1 A0' --expect '62 F1 A0 02'
Response [4] 62 F1 A0 02 (positive response)
> raw '22 F1 86' --expect '62 F1 86 01'
Response [4] 62 F1 86 01 (positive response)
> read-did 0xF110 0xF111 0xF112 0xF189 0xF195
0xF110 CalibrationVersion [3] 01 00 00  Major=1, Minor=0, Patch=0
0xF111 BootloaderVersion [3] 00 00 00  Major=0, Minor=0, Patch=0
0xF112 HardwareVersion [3] 00 00 00  Major=0, Minor=0, Patch=0
0xF189 SoftwareVersion [3] 01 01 01  Major=1, Minor=1, Patch=1
0xF195 SupplierSoftwareVersion [3] 01 01 01  Major=1, Minor=1, Patch=1
> read-did 0xF183 0xF184 0xF18C 0xF190
0xF183 BootFingerprint [12] 00 00 00 00 00 00 00 00 00 00 00 00  Fingerprint=""
0xF184 AppFingerprint [12] 00 00 00 00 00 00 00 00 00 00 00 00  Fingerprint=""
0xF18C SerialNumber [12] 00 00 00 00 00 00 00 00 00 00 00 00  Serial=""
0xF190 VIN [17] 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  VIN=""
> read-did 0xFD10 0xFD11
0xFD10 PhaseCurrents [6] 00 2C 00 30 00 44  U=4.4 A, V=4.8 A, W=6.8 A
0xFD11 SensorAddresses [4] 00 00 00 00  U=0, V=0, W=0, Found=no
> raw '22 FE ED' --expect '7F 22 31'
Response [3] 7F 22 31 (negative response 0x31 (RequestOutOfRange))
> raw '22 F1' --expect '7F 22 13'
Response [3] 7F 22 13 (negative response 0x13 (IncorrectMessageLengthOrInvalidFormat))
> raw '10 05' --expect '7F 10 12'
Response [3] 7F 10 12 (negative response 0x12 (SubFunctionNotSupported))
> raw '19 02 FF' --expect '59 02'
Response [2] 59 02 (positive response)
> raw '19 02' --expect '7F 19 13'
Response [3] 7F 19 13 (negative response 0x13 (IncorrectMessageLengthOrInvalidFormat))
> session extended
Session 0x03 (extended) active. P2 = 100 ms, P2* = 2000 ms.
> raw '22 F1 86' --expect '62 F1 86 03'
Response [4] 62 F1 86 03 (positive response)
> functional '3E 80' --expect none
No response
> sleep 6
> raw '22 F1 86' --expect '62 F1 86 03'
Response [4] 62 F1 86 03 (positive response)
> session default
Session 0x01 (default) active. P2 = 100 ms, P2* = 2000 ms.
> functional '10 83' --expect none
No response
> raw '22 F1 86' --expect '62 F1 86 03'
Response [4] 62 F1 86 03 (positive response)
> functional '10 01' --expect '50 01 00 64 00 C8'
Response [6] 50 01 00 64 00 C8 (positive response)
> raw '22 F1 86' --expect '62 F1 86 01'
Response [4] 62 F1 86 01 (positive response)
```

Exit status 0 means every line got the answer it expected. Note `sleep 6`: it
waits longer than S3, yet the board stays in the extended session because the
shell keeps sending TesterPresent in the background.

The phase currents are live values: B19 reads a few amperes with no current
flowing, and they change from one read to the next.

### Step 5: the automated checks

The same kind of checks, as a pytest suite. On hardware it runs only the checks
that are safe on a real board; the others are skipped.

```powershell
python -m pytest tests\test_board_checks.py --board-profile profiles\test_board.toml -v
```

**Output (B19)**, shortened where marked `...`:

```text
collected 32 items

tests/test_board_checks.py::test_tester_present PASSED                                   [  3%]
tests/test_board_checks.py::test_application_image_is_active PASSED                      [  6%]
tests/test_board_checks.py::test_default_session_after_previous_test PASSED              [  9%]
...
tests/test_board_checks.py::test_read_only_did_write_is_rejected SKIPPED (simulation only: not sent to hardware) [ 62%]
tests/test_board_checks.py::test_session_control_reports_timing_and_active_session[1] PASSED [ 65%]
...
tests/test_board_checks.py::test_functional_tester_present_suppressed PASSED             [ 96%]
tests/test_board_checks.py::test_pre_programming_sequence SKIPPED (simulation only: not sent to hardware) [100%]

================================ 27 passed, 5 skipped in 9.37s ================================
```

The five skipped checks write a DID, enter the programming session or disable
communication, so they run only on the simulator. Without `--board-profile`, all
32 run against the simulator. Leave out `-v` for one dot per check instead of
one line.

---

## 5. UDS command reference

All commands take the bus and ISO-TP options (or `--ecu`). The examples use
`@tb` from section 3.

### session: DiagnosticSessionControl (0x10)

```powershell
ecu-can uds session extended @tb
```

**Output (B19)**

```text
Session 0x03 (extended) active. P2 = 100 ms, P2* = 2000 ms.
```

Accepts `default`, `programming`, `extended` or a number (`0x40`). The timing
values are the ones the ECU reports. The session lasts only while the ECU keeps
hearing from the tester (see section 2, item 5).

### tester-present: TesterPresent (0x3E)

```powershell
ecu-can uds tester-present @tb
```

```text
Tester present OK
```

### read-did: ReadDataByIdentifier (0x22)

One request per DID, in order. Give each DID as a number or as a name from the
profile's `[dids]` table (section 3):

```powershell
ecu-can uds read-did PhaseCurrents SensorAddresses vin @tb
```

**Output (B19)**

```text
0xFD10 PhaseCurrents [6] 00 31 00 2D 00 47  U=4.9 A, V=4.5 A, W=7.1 A
0xFD11 SensorAddresses [4] 00 00 00 00  U=0, V=0, W=0, Found=no
0xF190 VIN [17] 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  VIN=""
```

A DID the table does not name prints as bytes. When the board sends a length
the table does not expect, the line ends in
`(cannot decode: expected 6 bytes, got 2)` instead of a guess.

Values are printed as they arrive; the command stops at the first refusal:

```powershell
ecu-can uds read-did 0xFEED @tb
```

**Output (B19)**

```text
ecu-can: negative response 0x31 (RequestOutOfRange)
```

### write-did: WriteDataByIdentifier (0x2E)

```powershell
ecu-can uds write-did 0xF184 "41 42 43 44 45 46 47 48 49 4A 4B 4C" @tb
```

The value is given as hex bytes and must have the length the ECU expects. The
DID can be a name from the profile, for example `write-did AppFingerprint "..."`.
Writes usually need the extended session and an unlocked security level, so in
practice you run them from the shell (see the example in section 6).

### reset: ECUReset (0x11)

```powershell
ecu-can uds reset soft @tb
```

**Output (simulator, B19 pending)**

```text
ECU reset 0x03 accepted
```

Accepts `hard`, `key-off-on`, `soft` or a number. The board restarts in the
default session and locked.

### routine: RoutineControl (0x31)

```powershell
ecu-can uds routine start 0xFF00 "01 02" @tb
ecu-can uds routine result 0xFF00 @tb
```

Takes `start`, `stop`, `result` or a number, the routine ID, and an optional
option record. It prints the routine status record the ECU returns.

> **B19:** routine 0xFD00 sends transactions to the current sensors and can
> write their EEPROM, which cannot be undone. Never run it on a board fitted
> with sensors. B19 has its three sensors fitted, so 0xFD00 is not sent to it.

### unlock: SecurityAccess (0x27)

```powershell
ecu-can uds unlock 1 @tb
```

Requests a seed for the level (odd numbers: 1, 3, ...), computes the key and
sends it. Needs the secret key (section 7) and, on B19, the extended session.
Run on its own in the default session, it is refused:

**Output (B19)**

```text
ecu-can: negative response 0x7F (ServiceNotSupportedInActiveSession)
```

Run it from the shell after `session extended` (section 6). Here the four
commands are piped into the shell, with the key in a file outside the project:

```powershell
"session extended", "unlock 1", "unlock 1", "session default" | ecu-can uds shell @tb --key-file C:\keys\test_board.key
```

**Output (B19)**

```text
> session extended
Session 0x03 (extended) active. P2 = 100 ms, P2* = 2000 ms.
> unlock 1
Security level 0x01 unlocked
> unlock 1
Security level 0x01 already unlocked (zero seed)
> session default
Session 0x01 (default) active. P2 = 100 ms, P2* = 2000 ms.
```

The second `unlock 1` gets an all-zero seed: the level is already open, so no
key is sent. Returning to the default session locks the board again.

> **B19:** level 1 unlocks only on a development build. On a standard build the
> board answers the key with NRC 0x22 (ConditionsNotCorrect), whatever the key.

### raw: any request

Sends the bytes as given and prints the whole response, positive or negative.

```powershell
ecu-can uds raw "22 F1 86" @tb
```

**Output (B19)**

```text
Response [4] 62 F1 86 01 (positive response)
```

A negative response is printed and the exit status is 1:

```powershell
ecu-can uds raw "22 FE ED" @tb
```

**Output (B19)**

```text
Response [3] 7F 22 31 (negative response 0x31 (RequestOutOfRange))
```

**`--expect`** turns `raw` into a check. It passes when the response starts with
the given bytes, so a negative response can be the expected result:

```powershell
ecu-can uds raw "22 FE ED" --expect "7F 22 31" @tb
```

**Output (B19)**

```text
Response [3] 7F 22 31 (negative response 0x31 (RequestOutOfRange))
```

Exit status 0. When the response differs, the command says what it wanted:

```powershell
ecu-can uds raw "22 F1 86" --expect "62 F1 86 03" @tb
```

**Output (B19)**

```text
Response [4] 62 F1 86 01 (positive response)
ecu-can: expected a response starting 62 F1 86 03, got 62 F1 86 01
```

`--expect none` passes only when no response comes back.

### functional: a request to every ECU

Sends one single frame to the functional ID (0x601 on B19) and lists every
answer within `--listen` seconds (default 0.5). A single frame carries at most
7 bytes on classic CAN.

```powershell
ecu-can uds functional "10 01" @tb
```

**Output (B19)**

```text
Response [6] 50 01 00 64 00 C8 (positive response)
```

With the "suppress positive response" bit (0x80) set, no answer is the correct
result:

```powershell
ecu-can uds functional "3E 80" --expect none @tb
```

**Output (B19)**

```text
No response
```

Without it, the ECU answers:

```powershell
ecu-can uds functional "3E 00" @tb
```

**Output (B19)**

```text
Response [2] 7E 00 (positive response)
```

### dtc: diagnostic trouble codes (0x19, 0x14)

Four sub-commands:

| Command | Request | Shows |
|---|---|---|
| `dtc count [--mask M]` | `19 01 M` | how many DTCs have a status bit in `M` |
| `dtc read [--mask M]` | `19 02 M` | those DTCs, with their status |
| `dtc supported` | `19 0A` | every DTC the ECU knows, with its status |
| `dtc clear [GROUP]` | `14 GG GG GG` | clears DTCs: all (`0xFFFFFF`, the default), a group or one DTC |

The mask defaults to `0xFF`: a DTC matches when any of its status bits is set.
`--mask 0x08` gives the confirmed DTCs, `--mask 0x01` those failing now.

**B19 has no DTCs yet.** Its firmware answers every ReadDTCInformation request
with only `59` and the sub-function. ISO 14229-1 requires at least the status
availability mask, so the tool says what is missing instead of failing:

```powershell
ecu-can uds dtc read --mask 0x08 @tb
```

**Output (B19)**

```text
Response [2] 59 02: no status availability mask and no DTCs (shorter than ISO 14229-1 allows)
```

`dtc count` and `dtc supported` print the same kind of line on B19
(`Response [2] 59 01: no status availability mask and no count ...`), with exit
status 0: the board did answer positively.

**What an ECU with stored DTCs returns.** The generic simulated ECU (section 8)
has three made-up DTCs. The test suite runs `dtc read` against it in one Python
process. With two adapters on one bench bus, `ecu-can uds simulate --interface
pcan --channel PCAN_USBBUS1 --tx-id 0x7E0 --rx-id 0x7E8` plays the same ECU for
a tester on the other adapter with the same IDs.

**Output (simulator: the generic ECU)** of `dtc read`

```text
Status availability mask 0xFF
3 DTCs with status & 0xFF:
  0x0A1B12 P0A1B-12  status 0x24 pendingDTC testFailedSinceLastClear
  0x9E0013 B1E00-13  status 0x50 testNotCompletedSinceLastClear testNotCompletedThisOperationCycle
  0xC07300 U0073-00  status 0x09 testFailed confirmedDTC
```

Each line is the 3-byte DTC in hex, the same DTC as an SAE J2012 code (letter
P, C, B or U, four characters, then the failure type), and the status byte with
the names of its set bits:

| Bit | Value | Name |
|---|---|---|
| 0 | 0x01 | testFailed |
| 1 | 0x02 | testFailedThisOperationCycle |
| 2 | 0x04 | pendingDTC |
| 3 | 0x08 | confirmedDTC |
| 4 | 0x10 | testNotCompletedSinceLastClear |
| 5 | 0x20 | testFailedSinceLastClear |
| 6 | 0x40 | testNotCompletedThisOperationCycle |
| 7 | 0x80 | warningIndicatorRequested |

`dtc count --mask 0x08` against the same ECU:

```text
1 DTC with status & 0x08 (availability mask 0xFF, format 0x01 ISO 14229-1)
```

`dtc clear` changes the ECU: it erases the DTCs and their stored data.

```powershell
ecu-can uds dtc clear @tb
```

**Output (B19)**

```text
Clear accepted (all DTC groups)
```

"Accepted" because the tool only sees the answer `54`: B19 sends it, but its
firmware clears nothing.
After a clear the simulated DTCs read `status 0x50` (not completed since the
clear). A DTC the ECU does not know gives
`negative response 0x31 (RequestOutOfRange)`.

For the other ReadDTCInformation sub-functions (snapshots `19 04`, extended
data `19 06`, ...) use `raw`, e.g. `ecu-can uds raw "19 04 C0 73 00 FF" @tb`.

### Common options

| Option | Meaning | Default |
|---|---|---|
| `--ecu FILE` | load a profile | none |
| `--interface virtual\|pcan` | backend | virtual |
| `--channel NAME` | adapter channel | PCAN_USBBUS1 |
| `--bitrate N` | nominal bit/s | 500000 |
| `--tx-id`, `--rx-id` | request and response IDs | from the profile |
| `--functional-id` | functional request ID | from the profile |
| `--padding 0xAA\|none` | frame padding byte | 0xCC |
| `--timeout S` | seconds to wait for a response | 2 |
| `--log FILE` | record every frame sent and received (section 6) | off |
| `-v` (before `uds`) | show ISO-TP and UDS library logs | off |

---

## 6. Scripts and the UDS shell

The shell keeps **one connection open** and sends TesterPresent (`3E 80`) every
2 seconds in the background, so the session and security level hold between
commands. It accepts the same commands as section 5 without the bus options,
plus:

| Command | Meaning |
|---|---|
| `sleep SECONDS` | wait (TesterPresent keeps running) |
| `help` | list the commands |
| `quit` | leave the shell |
| `# text` | comment, to the end of the line |

### Typing commands

```powershell
ecu-can uds shell @tb
```

You get a `uds>` prompt. A failed command prints its error and the shell keeps
going. Leave with `quit` or Ctrl+Z then Enter.

### Running a script

A script is a text file with one command per line, usually with the `.uds`
extension. The shell prints each line before running it and **stops at the
first failure** with exit status 1, so a script is also a test.

For a few commands you don't need a file: pipe them in as quoted strings
separated by commas, and they run the same way. Section 5 (`unlock`) has an
example on B19.

Example `scripts\my_write.uds`, which unlocks, writes a DID and reads it back:

```text
session extended
unlock 1
read-did 0xF184
write-did 0xF184 "41 42 43 44 45 46 47 48 49 4A 4B 4C"
read-did 0xF184
```

```powershell
ecu-can uds shell @tb scripts\my_write.uds
```

**Output (simulator; B19 not planned, writes are skipped on hardware)**

```text
> session extended
Session 0x03 (extended) active. P2 = 100 ms, P2* = 2000 ms.
> unlock 1
Security level 0x01 unlocked
> read-did 0xF184
0xF184 AppFingerprint [12] 41 50 50 4C 2D 46 50 2D 30 30 30 31  Fingerprint="APPL-FP-0001"
> write-did 0xF184 '41 42 43 44 45 46 47 48 49 4A 4B 4C'
0xF184 written [12] 41 42 43 44 45 46 47 48 49 4A 4B 4C
> read-did 0xF184
0xF184 AppFingerprint [12] 41 42 43 44 45 46 47 48 49 4A 4B 4C  Fingerprint="ABCDEFGHIJKL"
```

Running `unlock 1` a second time prints
`Security level 0x01 already unlocked (zero seed)`: the ECU sends an all-zero
seed when the level is already open (B19 does the same, see section 5).

### When a script stops

The failing line is named, and nothing after it runs:

```text
> tester-present
Tester present OK
> read-did 0xF1A0
0xF1A0 ActiveImage [1] 02  Image=application
> session programing
ecu-can: line 3: uds> session: argument session: expected a decimal or 0x-prefixed integer
ecu-can: stopped at line 3
```

Here `programing` is misspelt, so it was read as a number and rejected.

### Writing good scripts

- Put `--expect` on `raw` lines so the script checks results rather than only
  printing them.
- Start with `session extended` when later lines need it; end with
  `session default` to leave the board as you found it.
- Use `--keep-alive 0` on the shell command line to test what the board does
  without TesterPresent, for example the S3 timeout.

### Recording a session: --log

`--log FILE` on any `uds` command, or on the shell, records every frame of the
conversation: requests, flow control, responses, functional requests and the
shell's TesterPresent. The extension picks the format: `.asc`, `.blf`, `.csv`
or `.log`. Read it back with `ecu-can decode` (section 10), or open it in a
CAN tool. Sent and received frames share one clock: on a PEAK adapter both are
stamped by the adapter.

```powershell
ecu-can uds shell @tb scripts\test_board_readonly.uds --log C:\logs\b19_readonly.blf
ecu-can decode C:\logs\b19_readonly.blf --relative
```

**Output (B19)** of the `decode` command, shortened to its first 12 lines:

```text
67 of 67 frames shown
0.000000 0x60A STD CAN [8] 02 3E 00 AA AA AA AA AA
0.001278 0x68A STD CAN [8] 02 7E 00 AA AA AA AA AA
0.003194 0x60A STD CAN [8] 03 22 F1 A0 AA AA AA AA
0.004980 0x68A STD CAN [8] 04 62 F1 A0 02 AA AA AA
0.006852 0x60A STD CAN [8] 03 22 F1 86 AA AA AA AA
0.009289 0x68A STD CAN [8] 04 62 F1 86 01 AA AA AA
0.011059 0x60A STD CAN [8] 03 22 F1 10 AA AA AA AA
0.013791 0x68A STD CAN [8] 06 62 F1 10 01 00 00 AA
0.015694 0x60A STD CAN [8] 03 22 F1 11 AA AA AA AA
0.016300 0x68A STD CAN [8] 06 62 F1 11 00 00 00 AA
0.017434 0x60A STD CAN [8] 03 22 F1 12 AA AA AA AA
0.019886 0x68A STD CAN [8] 06 62 F1 12 00 00 00 AA
```

Requests (0x60A) and responses (0x68A) alternate, each answer 1 to 4 ms after
its request. `67 of 67 frames shown` goes to the error stream, so it can come
first. Without `--relative` the times are the date and time of the run.

Keep logs outside the project folder: they hold whatever the board sent.

---

## 7. SecurityAccess keys

`unlock` needs the board's secret key. **The key is never stored in the
repository**: `*.key` files and `keys\` folders are ignored by git, and the
profile leaves `key_file` commented out. Keep the key in a folder outside the
project, for example `C:\keys\`.

The tool looks for the secret in this order:

1. `--key-file PATH` on the command line;
2. `key_file` in the profile's `[security]` section (a relative path is resolved
   from the profile's folder);
3. the `ECU_COMM_SECURITY_KEY` environment variable.

The key file holds the secret as hex text; spaces, colons and a `0x` prefix are
ignored. To set the variable for the current PowerShell window only:

```powershell
$env:ECU_COMM_SECURITY_KEY = Get-Content C:\keys\test_board.key
```

Without a secret, `unlock` stops with a message naming
`ECU_COMM_SECURITY_KEY`.

**Algorithms.** The default `aes128-ecb` computes key = AES-128-ECB(secret,
seed). For another ECU, write a Python function
`my_function(seed, level, secret) -> bytes` in your own module and select it
with `--key-algorithm mypackage.mymodule:my_function`, or with `algorithm` in
the profile.

**Wrong keys.** B19 allows three wrong keys (NRC 0x35, 0x35, then 0x36) and then
refuses for about 10 seconds (NRC 0x37). Wait before trying again.

---

## 8. Working without hardware

- `ecu-can demo` and `ecu-can uds demo` check the installation (section 1).
- The **test suite** runs every command against a simulated generic ECU (with
  three made-up DTCs) and a simulated test_board:

  ```powershell
  python -m pytest
  ```

- `profiles\test_board.dbc` and `examples\test_board_sample.asc` (an invented
  network and one second of its traffic) let you try every DBC command
  (section 10).
- `ecu-can uds simulate --board test_board` makes a PC behave like B19 on a real
  bus. It answers on a second adapter or channel of the same bus, so a tester on
  the first adapter cannot tell it from the board:

  ```powershell
  ecu-can uds simulate --board test_board --interface pcan --channel PCAN_USBBUS1
  ```

  Stop it with Ctrl+C. It uses a public throw-away secret unless you give a key.

> **Limitation:** the `virtual` backend connects programs only inside one Python
> process. A `simulate` command in one PowerShell window cannot talk to a tester
> in another window over `virtual`. Use `uds demo`, the tests, or two PCAN
> channels.

---

## 9. Raw CAN frames

For bus work below UDS. Display traffic for 10 seconds:

```powershell
ecu-can listen @tb --duration 10
```

Each frame prints as `timestamp ID flags [length] data`.

Send one frame (only on an isolated bench bus, with an ID nothing else uses):

```powershell
ecu-can send @tb 0x123 "01 02 AB"
```

CAN-FD needs `--fd` on the bus, and `--frame-fd` (plus `--brs` for the data
bitrate) on the frame. Classic CAN carries 0 to 8 bytes; CAN-FD carries 0 to 8,
12, 16, 20, 24, 32, 48 or 64.

---

## 10. DBC files: signals instead of bytes

A DBC file names the messages on a bus and describes their signals: where each
one sits in the frame, how to scale it, its unit and its value names. With a DBC,
`ecu-can` shows `SupplyVoltage=24.12 V` instead of `6C 09`, and builds frames
from values instead of hex.

The project includes an invented network, `profiles\test_board.dbc`, and one
second of recorded traffic for it, `examples\test_board_sample.asc`. Every
example in this section runs with them, without hardware. Define once per window:

```powershell
$d = "--dbc", "profiles\test_board.dbc"
```

> **Company DBC files stay outside the repository.** Give their full path with
> `--dbc`, or list them in a profile (see the end of this section).

### dbc show: what a DBC contains

```powershell
ecu-can dbc show @d BoardStatus 0x18FF0010
```

**Output (no hardware)**

```text
0x100 STD CAN [8] BoardStatus (cycle 100 ms; from TestBoard)
  State: bit 0, 4 bits, Intel, unsigned, [0 .. 3], 0=Init 1=Ready 2=Running 3=Fault
  Counter: bit 4, 4 bits, Intel, unsigned, [0 .. 15]
  SupplyVoltage: bit 8, 16 bits, Intel, unsigned, x0.01, [0 .. 655.35] V
  BoardTemperature: bit 24, 8 bits, Intel, signed, [-128 .. 127] degC
  FaultActive: bit 32, 1 bit, Intel, unsigned, [0 .. 1]

0x18FF0010 EXT CAN [8] BoardInfo (cycle 1000 ms; from TestBoard)
  Page: bit 0, 8 bits, Intel, unsigned, [0 .. 255], multiplexer
  SerialNumber: bit 8, 32 bits, Intel, unsigned, [0 .. 4294967295], when Page = 0
  SwMajor: bit 8, 8 bits, Intel, unsigned, [0 .. 255], when Page = 1
  SwMinor: bit 16, 8 bits, Intel, unsigned, [0 .. 255], when Page = 1
```

Messages can be named or given by ID; with none, every message is listed.
Each signal line reads: start bit, length, byte order (Intel = little-endian,
Motorola = big-endian), sign, scaling (`x0.01` means physical = raw x 0.01, an
offset shows as `+40` or `-40`), range and unit, then any value names. BoardInfo
is *multiplexed*: the `Page` byte decides which signals the rest of the frame
holds.

### decode: a recorded log

```powershell
ecu-can decode @d examples\test_board_sample.asc
```

**Output (no hardware)**, shortened where marked `...`:

```text
0.000000 0x18FF0010 EXT CAN [8] 00 51 16 40 00 00 00 00  BoardInfo Page=0, SerialNumber=4200017
0.001000 0x100 STD CAN [8] 01 6C 09 1F 00 00 00 00  BoardStatus State=Ready, Counter=0, SupplyVoltage=24.12 V, BoardTemperature=31 degC, FaultActive=0
0.004000 0x200 STD CAN [6] 00 00 00 00 00 00  PhaseCurrents CurrentU=0.0 A, CurrentV=0.0 A, CurrentW=0.0 A
...
0.304000 0x200 STD CAN [6] 00 7B FF C2 FF C2  PhaseCurrents CurrentU=12.3 A, CurrentV=-6.2 A, CurrentW=-6.2 A
...
0.500000 0x18FF0010 EXT CAN [8] 01 02 07 00 00 00 00 00  BoardInfo Page=1, SwMajor=2, SwMinor=7
...
0.620000 0x7FF STD CAN [2] 01 02
...
0.750000 0x300 STD FD BRS [12] 01 B8 0B 00 00 00 00 00 90 01 00 00  TesterCommand Mode=Speed, SpeedRequest=1500.0 rpm, TorqueLimit=40.0 Nm
...
24 of 24 frames shown
```

Each line is the raw frame, two spaces, then the message name and its signals.
An ID the DBC doesn't know (0x7FF here) is shown raw. The last line goes to the
error stream, so it stays out of anything you redirect to a file.

- **Formats:** `.asc`, `.blf`, `.csv`, `.log`, `.trc` and `.db`; `.mf4` needs the
  `asammdf` package.
- **Times** are shown as logged. `--relative` counts from the first frame.
- Without `--dbc`, `decode` lists the raw frames.

**Filters.** `--message NAME` keeps only those messages; `--signal NAME` keeps
only those signals, and only the frames that carry them. Both can be repeated:

```powershell
ecu-can decode @d --signal State --signal CurrentU examples\test_board_sample.asc
```

**Output (no hardware)**, shortened:

```text
...
0.201000 0x100 STD CAN [8] 21 6A 09 1F 00 00 00 00  BoardStatus State=Ready
0.204000 0x200 STD CAN [6] 00 00 00 00 00 00  PhaseCurrents CurrentU=0.0 A
0.301000 0x100 STD CAN [8] 32 6C 09 1F 00 00 00 00  BoardStatus State=Running
...
0.904000 0x200 STD CAN [6] 00 81 FF C0 FF C0  PhaseCurrents CurrentU=12.9 A
20 of 24 frames shown
```

**A table for Excel.** `--csv FILE` writes one row per signal value instead:

```powershell
ecu-can decode @d --message PhaseCurrents --csv currents.csv examples\test_board_sample.asc
```

**Output (no hardware)**: the summary, then the first rows of `currents.csv`:

```text
10 of 24 frames shown, written to currents.csv
```

```text
time,id,message,signal,value,unit
0.004000,0x200,PhaseCurrents,CurrentU,0.0,A
0.004000,0x200,PhaseCurrents,CurrentV,0.0,A
0.004000,0x200,PhaseCurrents,CurrentW,0.0,A
0.104000,0x200,PhaseCurrents,CurrentU,0.0,A
```

### listen --dbc: live traffic

`listen` takes the same `--dbc`, `--message` and `--signal` options and prints
the same lines as `decode`, as frames arrive. `--count` counts the frames shown.
Add `--log FILE` to record every received frame at the same time (`.asc`,
`.blf`, `.csv` or `.log`); the filters change only what is shown, not what is
recorded. Decode the recording later with `decode`.

```powershell
ecu-can listen @tb --dbc C:\networks\board.dbc --duration 60 --log C:\logs\run1.blf
ecu-can decode --dbc C:\networks\board.dbc --csv C:\logs\run1.csv C:\logs\run1.blf
```

`listen` never transmits. On B19 the application messages are on the second
adapter (`--channel PCAN_USBBUS1`), not on the diagnostic bus.

### dbc encode and dbc send: frames from values

`dbc encode` builds a frame and prints it; it needs no bus.

```powershell
ecu-can dbc encode @d BoardStatus State=Running SupplyVoltage=24.12
ecu-can dbc encode @d BoardInfo Page=1 SwMajor=2 SwMinor=7
```

**Output (no hardware)**

```text
0x100 STD CAN [8] 02 6C 09 00 00 00 00 00  BoardStatus State=Running, Counter=0, SupplyVoltage=24.12 V, BoardTemperature=0 degC, FaultActive=0
0x18FF0010 EXT CAN [8] 01 02 07 00 00 00 00 00  BoardInfo Page=1, SwMajor=2, SwMinor=7
```

- **Values** are numbers (`24.12`, `-3`, `0x1F`) or value names (`State=Running`).
- **Signals you leave out** take their DBC start value, else 0. The printed line
  always shows every signal, so check it.
- **Multiplexed messages:** give the multiplexer (`Page=1`); only that page's
  signals are filled in.

Anything that cannot be sent exactly as written is refused, so nothing is
silently changed. A value outside the DBC range is refused too.

```powershell
ecu-can dbc encode @d BoardStatus Counter=1.5
ecu-can dbc encode @d BoardStatus State=Stopped
ecu-can dbc encode @d BoardInfo SwMajor=2
```

**Output (no hardware)**

```text
ecu-can: Counter=1.5 is not a multiple of its resolution 1
ecu-can: State has no value named Stopped (Init, Ready, Running, Fault)
ecu-can: SwMajor is only sent when Page = 1: give Page=... as well
```

`dbc send` takes the same arguments plus the bus options, and **transmits**. Use
it only on a bus where that message is expected, such as a bench bus with a
simulator. `--count N` repeats the frame every `--period` seconds (default: the
DBC cycle time); `--brs` switches the data bitrate of a CAN-FD message.

```powershell
ecu-can dbc send @d BoardStatus State=Ready SupplyVoltage=24.1 --count 3 --period 0.1
```

**Output (no hardware)**, on the default virtual bus:

```text
Sent x3, every 100 ms 0x100 STD CAN [8] 01 6A 09 00 00 00 00 00  BoardStatus State=Ready, Counter=0, SupplyVoltage=24.10 V, BoardTemperature=0 degC, FaultActive=0
```

### DBC files in a profile

List the DBC files in the profile instead of typing `--dbc` each time. A
relative path is resolved from the profile's folder; `--dbc` on the command line
replaces the list.

```toml
[dbc]
files = ["C:/networks/board.dbc", "C:/networks/charger.dbc"]
```

Several files are loaded together. Two files defining the same ID is an error
naming both, rather than one silently replacing the other:

**Output (no hardware)**, for `ecu-can dbc show @d --dbc profiles\test_board.dbc`:

```text
ecu-can: profiles\test_board.dbc: 0x100 (BoardStatus) is already defined as BoardStatus in profiles\test_board.dbc
```

---

## 11. Troubleshooting

| Message | Meaning | What to do |
|---|---|---|
| `no response from the ECU before the timeout` | nothing answered on the response ID | Check 24 V, the cable on PT-CAN, the channel name, the bitrate and the IDs. |
| `The driver is not loaded` | Windows sees no PEAK adapter on that channel | Check the USB cable and that the adapter shows under "CAN-Hardware" in Device Manager; install PEAK's device driver if it never appears. |
| `uptime library not available, timestamps are relative to boot time ...` | the venv was installed before the framework required `uptime` | Harmless (frame times count from Windows start-up instead of the date and time). Reinstall to remove it: `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`. |
| `UDS needs --tx-id and --rx-id, or an --ecu profile that sets them` | no addressing given | Add `--ecu` or the two IDs. |
| `negative response 0x7F (ServiceNotSupportedInActiveSession)` | wrong session | Use the shell with `session extended` first. |
| `negative response 0x33 (SecurityAccessDenied)` | level is locked | `unlock 1` first, in the same shell. |
| `negative response 0x22 (ConditionsNotCorrect)` | the board refuses in its current state | On B19's 0xFD00: the PWM is on. |
| `negative response 0x35` / `0x36` / `0x37` | wrong key; attempts exceeded; delay running | Check the secret; wait 10 s. |
| `unknown key isotp.txid` (or similar) | typo in a profile | Fix the key; `profiles\example_ecu.toml` lists them all. |
| `functional requests need --functional-id ...` | no functional ID | Add it to the profile or the command. |
| `stopped at line N` | a script line failed | The line before it says why. |
| `no DID named X (names: ...)` | `read-did` / `write-did` name not in the profile | Use one of the listed names, or the number. |
| `(cannot decode: expected 6 bytes, got 2)` | the board sent another length than the `[dids]` table says | Check the table against the software on the board. |
| `dids.0xF190: ...` | a mistake in the profile's `[dids]` table | The message names the DID and the field. |
| `... is already defined as ... in ...` | two DBC files define the same ID | Load only one of them, or fix the files. |
| `cannot decode: Wrong data size: 2 instead of 8 bytes` | the frame is shorter than its DBC message | Check that the DBC matches the software on the board. |
| `no DBC files: give --dbc FILE, ...` | a DBC command without a DBC | Add `--dbc` or an `--ecu` profile with `[dbc]`. |
| `no status availability mask and no DTCs (shorter than ISO 14229-1 allows)` | the ECU answered `59 xx` and nothing more | Not an error of the tool. On B19 this is the firmware's placeholder; it has no DTCs. |
| `... is not a multiple of its resolution ...` | `dbc encode` / `dbc send` value cannot be sent exactly | Round it to the signal's step (`dbc show` lists it). |

For a closer look at what goes on the bus, put `-v` before `uds`:
`ecu-can -v uds tester-present @tb`.

---

## 12. Reference tables

### Negative response codes seen on B19

| NRC | Name | Typical cause |
|---|---|---|
| 0x11 | ServiceNotSupported | the service does not exist on this ECU |
| 0x12 | SubFunctionNotSupported | e.g. session 0x05 |
| 0x13 | IncorrectMessageLengthOrInvalidFormat | request too short or too long |
| 0x22 | ConditionsNotCorrect | state does not allow it |
| 0x31 | RequestOutOfRange | unknown DID or RID, or a value out of range |
| 0x33 | SecurityAccessDenied | level locked |
| 0x35 | InvalidKey | wrong key |
| 0x36 | ExceededNumberOfAttempts | too many wrong keys |
| 0x37 | RequiredTimeDelayNotExpired | wait before the next seed |
| 0x78 | ResponsePending | the tool waits automatically |
| 0x7F | ServiceNotSupportedInActiveSession | change session first |

For functional requests the board stays silent instead of sending 0x11, 0x12,
0x31, 0x7E or 0x7F.

### B19 identifiers

| ID | What |
|---|---|
| 0x60A | physical requests (tester to board) |
| 0x68A | responses |
| 0x601 | functional requests |
| 0xF186 | ActiveSession (01 default, 02 programming, 03 extended) |
| 0xF1A0 | ActiveImage (02 application) |
| 0xF110, 0xF111, 0xF112, 0xF189, 0xF195 | CalibrationVersion, BootloaderVersion, HardwareVersion, SoftwareVersion, SupplierSoftwareVersion: 3 bytes each |
| 0xF183, 0xF184 | BootFingerprint, AppFingerprint |
| 0xF18C, 0xF190 | SerialNumber, VIN |
| 0xFD10, 0xFD11 | PhaseCurrents (0.1 A), SensorAddresses |

The names are the ones in `profiles\test_board.toml`.
