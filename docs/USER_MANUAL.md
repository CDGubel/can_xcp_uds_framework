# ecu-can User's Manual

`ecu-can` is a command-line tool for bench work on CAN and CAN-FD: raw frames,
UDS diagnostics over ISO-TP, scripted test sequences, and a simulated ECU for
work without hardware. This manual takes you from installation to running your
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
10. [Troubleshooting](#10-troubleshooting)
11. [Reference tables](#11-reference-tables)

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
3. **Output format.** `[n]` is a length in bytes. DID values print as hex, plus
   the text in quotes when every byte is printable.
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
0xF1A0 [1] 02
0xF186 [1] 01
```

`0xF1A0 = 02` means the application is running (not the bootloader), and
`0xF186 = 01` means the default session.

### Step 3: identification

```powershell
ecu-can uds read-did 0xF110 0xF111 0xF112 0xF189 0xF195 @tb
```

**Output (B19)**

```text
0xF110 [3] 01 00 00
0xF111 [3] 00 00 00
0xF112 [3] 00 00 00
0xF189 [3] 01 01 01
0xF195 [3] 01 01 01
```

These are the version DIDs, three bytes each. On B19 the fingerprints, serial
number and VIN (0xF183, 0xF184, 0xF18C, 0xF190) read as all zeros because they
have not been programmed. Zeros are not printed as text.

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
0xF110 [3] 01 00 00
0xF111 [3] 00 00 00
0xF112 [3] 00 00 00
0xF189 [3] 01 01 01
0xF195 [3] 01 01 01
> read-did 0xF183 0xF184 0xF18C 0xF190
0xF183 [12] 00 00 00 00 00 00 00 00 00 00 00 00
0xF184 [12] 00 00 00 00 00 00 00 00 00 00 00 00
0xF18C [12] 00 00 00 00 00 00 00 00 00 00 00 00
0xF190 [17] 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
> read-did 0xFD10 0xFD11
0xFD10 [6] 00 0D 00 24 00 3F
0xFD11 [4] 00 00 00 00
> raw '22 FE ED' --expect '7F 22 31'
Response [3] 7F 22 31 (negative response 0x31 (RequestOutOfRange))
> raw '22 F1' --expect '7F 22 13'
Response [3] 7F 22 13 (negative response 0x13 (IncorrectMessageLengthOrInvalidFormat))
> raw '10 05' --expect '7F 10 12'
Response [3] 7F 10 12 (negative response 0x12 (SubFunctionNotSupported))
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

One request per DID, in order. It stops at the first refusal.

```powershell
ecu-can uds read-did 0xFEED @tb
```

**Output (simulator, B19 pending)**

```text
ecu-can: negative response 0x31 (RequestOutOfRange)
```

### write-did: WriteDataByIdentifier (0x2E)

```powershell
ecu-can uds write-did 0xF184 "41 42 43 44 45 46 47 48 49 4A 4B 4C" @tb
```

The value is given as hex bytes and must have the length the ECU expects.
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

**Output (simulator, B19 pending)**

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

**Output (simulator, B19 pending)**

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

**Output (simulator, B19 pending)**

```text
Response [2] 7E 00 (positive response)
```

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
0xF184 [12] 41 50 50 4C 2D 46 50 2D 30 30 30 31 "APPL-FP-0001"
> write-did 0xF184 '41 42 43 44 45 46 47 48 49 4A 4B 4C'
0xF184 written [12] 41 42 43 44 45 46 47 48 49 4A 4B 4C
> read-did 0xF184
0xF184 [12] 41 42 43 44 45 46 47 48 49 4A 4B 4C "ABCDEFGHIJKL"
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
0xF1A0 [1] 02
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
- The **test suite** runs every command against a simulated generic ECU and a
  simulated test_board:

  ```powershell
  python -m pytest
  ```

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

## 10. Troubleshooting

| Message | Meaning | What to do |
|---|---|---|
| `no response from the ECU before the timeout` | nothing answered on the response ID | Check 24 V, the cable on PT-CAN, the channel name, the bitrate and the IDs. |
| `The driver is not loaded` | Windows sees no PEAK adapter on that channel | Check the USB cable and that the adapter shows under "CAN-Hardware" in Device Manager; install PEAK's device driver if it never appears. |
| `uptime library not available, timestamps are relative to boot time ...` | a python-can notice, printed once | Harmless; ignore it. |
| `UDS needs --tx-id and --rx-id, or an --ecu profile that sets them` | no addressing given | Add `--ecu` or the two IDs. |
| `negative response 0x7F (ServiceNotSupportedInActiveSession)` | wrong session | Use the shell with `session extended` first. |
| `negative response 0x33 (SecurityAccessDenied)` | level is locked | `unlock 1` first, in the same shell. |
| `negative response 0x22 (ConditionsNotCorrect)` | the board refuses in its current state | On B19's 0xFD00: the PWM is on. |
| `negative response 0x35` / `0x36` / `0x37` | wrong key; attempts exceeded; delay running | Check the secret; wait 10 s. |
| `unknown key isotp.txid` (or similar) | typo in a profile | Fix the key; `profiles\example_ecu.toml` lists them all. |
| `functional requests need --functional-id ...` | no functional ID | Add it to the profile or the command. |
| `stopped at line N` | a script line failed | The line before it says why. |

For a closer look at what goes on the bus, put `-v` before `uds`:
`ecu-can -v uds tester-present @tb`.

---

## 11. Reference tables

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
| 0xF186 | active session (01 default, 02 programming, 03 extended) |
| 0xF1A0 | running image (02 application) |
| 0xF110, 0xF111, 0xF112, 0xF189, 0xF195 | version DIDs, 3 bytes each |
| 0xF183, 0xF184 | boot and application fingerprints |
| 0xF18C, 0xF190 | serial number, VIN |
| 0xFD10, 0xFD11 | phase currents, sensor addresses |
