# DATAPixx USB Failure Harness

This harness makes a DATAPixx USB failure observable without opening a display. It sends scheduled digital-output conditions through a child process, logs every attempt and recovery event, and supplies controlled provocations for a real lab run.

> **Safety:** Run soak and provocation tests with no participant in the helmet or scanner room. Do not use an unattended hardware run as a safety or timing qualification.

The harness is based on pgl upstream commit `55ae664c632af828a100d8e94dd6d20832ad1ad3`. It does not modify `pgl/`.

## Setup and scope

Run commands from the repository root with Python 3.9 or later. The display-free harness uses the standard library and `pytest`; its two visual examples also need NumPy, Pillow, the macOS pgl build, a Metal-capable display, and `pypixxlib` with an attached DATAPixx. `system_profiler` is needed only for live macOS USB-topology collection.

The simulation path needs neither a display nor a DATAPixx. It replaces `pypixxlib._libdpx` only inside the worker process. A real run uses the installed `pypixxlib`, so run it only on the intended Mac with the intended USB chain connected.

Create an output directory before a command that writes there:

```bash
mkdir -p logs
```

## Start with the safe checks

Run the automated suite and render the supplied topology fixture before touching hardware:

```bash
python3 -m pytest harness/tests -v
python3 -m harness.usb_topology --input-json harness/tests/fixtures/system_profiler_usb.json
```

Run a short simulated soak to check the worker, CSV, and trigger sequence. Codes start at 1, wrap at `(2**num_bits)-1`, and never use zero except when an external caller deliberately wants all lines low.

```bash
python3 -m harness.soak --simulate --interval 0.1 --max-triggers 10 --csv logs/simulated.csv --text-log logs/simulated.log
```

Simulation can also exercise the recovery controller. This short run injects a mock USB error after a finite number of mock calls, records the failed trigger, terminates the failed worker, and retries open until the duration expires:

```bash
python3 -m harness.soak --simulate --simulate-fail-after-calls 7 --simulate-mode error --interval 0.05 --reopen-interval 0.05 --duration 0.5 --csv logs/simulated-recovery.csv --text-log logs/simulated-recovery.log
```

`--simulate-mode hang` tests the call deadline and worker termination; `silent` tests a healthy-looking device that emits no mock pulse. Keep `--duration` or `--max-triggers` finite for either mode.

```bash
python3 -m harness.soak --simulate --simulate-fail-after-calls 7 --simulate-mode hang --call-timeout 0.1 --reopen-interval 0.05 --duration 0.5 --csv logs/simulated-hang.csv
python3 -m harness.soak --simulate --simulate-fail-after-calls 7 --simulate-mode silent --interval 0.05 --reopen-interval 0.05 --duration 0.5 --csv logs/simulated-silent.csv
```

The corresponding normal hardware soak has no `--simulate` flag. It is a library-and-register communication test, not electrical verification:

```bash
python3 -m harness.soak --csv logs/baseline.csv --text-log logs/baseline.log
```

## Controlled provocations

Run one mode at a time. The first mode is the easiest place to begin because it only asks for one KVM switch; respond to each prompt only after the requested action. The hardware commands below are intentionally not simulation commands.

```bash
# Easiest first: switch the KVM once, then return the chain to its normal state.
python3 -m harness.provoke --kvm --duration 120 --csv logs/kvm.csv --text-log logs/kvm.log

# Power-cycle only the DATAPixx.
python3 -m harness.provoke --power --duration 120 --csv logs/power.csv

# Read, but never write, a large existing file on the USB volume under test.
python3 -m harness.provoke --bandwidth --bandwidth-source /Volumes/USB/large-file.bin --duration 120 --csv logs/bandwidth.csv

# Deliberately leave a helper's device handle open, then test a new worker's open.
python3 -m harness.provoke --stale-handle --duration 30 --csv logs/stale.csv

# Deliberately skip DPxWriteRegCache().
python3 -m harness.provoke --no-flush --max-triggers 1 --csv logs/no-flush.csv

# Perform the three prompted checks in order: Mac connector, DATAPixx connector, MSR feedthrough.
python3 -m harness.provoke --wiggle --duration 180 --csv logs/wiggle.csv
```

The bandwidth mode requires `--bandwidth-source` to name a non-empty, readable regular file. Use a path on the USB volume being investigated; the reader opens it read-only, rewinds at end of file, and marks the start, each GiB read, and the stop in the CSV. It creates and deletes nothing on that volume.

Useful simulated controls include a one-trigger no-flush run and the deliberate stale-handle crash:

```bash
python3 -m harness.provoke --no-flush --simulate --max-triggers 1 --csv logs/no-flush-simulated.csv
python3 -m harness.provoke --stale-handle --simulate --duration 0.5 --csv logs/stale-simulated.csv
```

For `--stale-handle`, a helper child opens the backend and exits with `os._exit(0)` without `DPxClose()`. The next worker immediately tries to reopen. In simulation, its persistent marker makes an expected refusal visible as `DPX_ERR_ALREADY_OPEN`; it is a deliberate model of a stale handle, not a claim about the macOS driver.

When any worker call misses `--call-timeout`, the parent records `worker_timeout`, terminates that child, and enters the reopen loop. Failed opens become `reopen_failed` markers; a healthy replacement produces `reopen_succeeded`. The CSV and signal handler remain in the parent, so a hung library call does not block their progress.

## USB topology and macOS logs

Collect the live macOS topology and preserve the raw profile alongside the run artifacts:

```bash
python3 -m harness.usb_topology --json-output logs/usb-topology.json
```

Use the offline form when reviewing a saved profile or working away from the instrument:

```bash
python3 -m harness.usb_topology --input-json logs/usb-topology.json
```

In a second terminal, capture the OS USB event stream during a hardware test:

```bash
log stream --style compact --predicate 'subsystem == "com.apple.iokit.IOUSBHostFamily"'
```

The parser flags a child whose reported `Current Required` exceeds the nearest parent's reported `Current Available`. It does not measure aggregate current or prove that a hub is electrically healthy.

## Visual hardware checks

These commands open Metal and a real DATAPixx. They are hardware-only and must not be used as automated smoke tests.

```bash
python3 -m harness.minimal_experiment --image testimage.jpg
python3 -m harness.things_task --image-dir /path/to/things/images
```

`minimal_experiment` prepares an 8-bit table, sends condition 17 before it creates and shows one image for 500 ms, then sends condition 18. Condition 17 is therefore a pre-image marker, not a measured visual-onset timestamp. `things_task` needs at least 200 supported image files, creates the first 200 textures before the task starts, displays each image for 0.5 seconds, and sends its one-based trial code at the image segment; the following 1.0-second fixation segment sends no task code.

## CSV timeline

Every row is flushed and `fsync`ed. This makes a crash lose at most the operation that was in progress. The columns are:

| Column | Meaning |
| --- | --- |
| `row_type` | `trigger` for an attempted condition, `marker` for an operator or recovery event. |
| `wall_timestamp` | Timezone-aware ISO 8601 wall time. |
| `monotonic_timestamp` | `time.monotonic()` value for ordering and duration calculations. |
| `device_time` | DATAPixx time returned by the backend, when available. |
| `sequence_number`, `trigger_code` | Monotonic attempt number and the condition code. |
| `error_code`, `error_string`, `ready` | Backend health at the end of the operation. |
| `latency_ms` | Parent-observed trigger round-trip time. |
| `outcome` | Result label described below. |
| `marker`, `detail` | Human action, recovery event, and supporting text. |

Correlate a failure by matching the sequence number and nearby monotonic time across the trigger row, the marker rows, the text log, the saved topology JSON, and the macOS log stream. The time bases differ, so use ordering and local timestamps rather than treating device time as a wall-clock timestamp.

### Outcome labels and what they prove

In simulation, `sent` means the mock observed a committed emission after the scheduled trigger. `no_emission` means no mock emission appeared; that covers both silent-drop behavior and no-flush behavior. The mock can also return `device_error`, or the parent can report `worker_timeout`, `worker_protocol_error`, or `worker_exception`.

On real hardware, a flushed, ready, error-free request is labeled `flushed_unverified`. It means the library accepted the schedule and `DPxWriteRegCache()` was called. It does **not** mean a voltage pulse appeared on the connector. A real `--no-flush` attempt is `uncommitted`, deliberately skips the register-cache write, and is electrically unverified. A mock silent mode has healthy `ready` and `DPX_SUCCESS` values but deliberately drops the emission; it tests control-path detection, not a physical silent failure.

## Test one hypothesis at a time

Keep the cable route, device firmware, trigger cadence, and downstream recording arrangement fixed unless the row says otherwise.

| Hypothesis | Change one variable | Result that supports it | Result that weakens it |
| --- | --- | --- | --- |
| KVM re-enumeration | Switch the KVM once during `--kvm`. | USB log shows remove/add and the CSV fails near the KVM markers or needs reopen. | No USB event, failure, or recovery change at the markers. |
| Bus bandwidth | Read only one large file from the target USB volume with `--bandwidth`. | Failures cluster after `bandwidth_started` or at a reproducible read load. | Baseline and read-load runs behave alike. |
| Bus power | Power-cycle only the DATAPixx with `--power`; compare saved topology before and after. | Re-enumeration, current warning, or recovery pattern tracks that cycle. | Stable topology and no change in failure behavior. |
| Stale handle | Run `--stale-handle`; change no cable or power state. | Immediate reopen refusal follows the deliberate child crash. | New worker opens cleanly and the failure needs another explanation. |
| Missing register flush | Run one `--no-flush` control. | Simulation reports `no_emission`; real hardware is deliberately `uncommitted`. | Any electrical conclusion from this control alone is invalid; add loopback measurement. |
| Cable, connectors, or feedthrough | Use `--wiggle` and perform only the prompted connector action in order. | Failure, USB event, or recovery tracks one connector's marker pair. | Repeated isolated actions show no temporal association. |
| Host-controller or hub topology | Move the unchanged DATAPixx chain to one documented alternate port or hub path and save both topology reports. | Failures occur only on one topology, or the report identifies its hub/current limit as the common factor. | Both documented paths behave alike under the same load and cadence. |
| Software recovery versus physical recovery | Keep the physical chain fixed and let the worker reopen; separately make one physical intervention. | Automatic reopen restores service without a physical change, or only a physical intervention restores it. | Both paths produce the same inconclusive result. |

The topology report is evidence about the host-controller/hub layout and reported current limits. It is not a bandwidth meter or a cable tester.

## Run logbook

Copy this header into a shared lab log. Record one row for every run, including controls with no failure.

| date/time | operator | command and Git SHA | CSV/log paths | topology snapshot | macOS / DATAPixx firmware / pypixxlib versions | trigger interval / planned duration / elapsed time | what single variable changed | failure and recovery | external pulse measurement |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |  |  |

## Experiment onset is a separate trigger

The inspected `pglExperiment.run()` implementation does not send digital output when it accepts the start key. It sets `state.experimentStarted`, calls `startPhase(0)`, prints the start message, and records `data.startTime`; `run()` itself sends no DATAPixx condition. `startPhase(0)` may start a task, and that task may independently send a task or segment trigger. That is not an automatic, distinct experiment-onset trigger.

For continuous MEG acquisition, reserve and send an explicit experiment-onset condition in the lab task. Verify that condition with the same external measurement path used for the recording system.

## What this environment cannot verify

The suite and simulation do not verify macOS platform and Metal presentation timing, electrical pulses, DATAPixx firmware behavior, physical USB recovery or re-enumeration, KVM behavior, cable/connector/feedthrough faults, or a populated local USB topology. The local live topology may be empty even when the offline fixture renders correctly.

On real hardware, ready/error checks establish only library and register communication. To establish an output pulse, observe it at the MEG input, a loopback, a logic analyzer, or an oscilloscope. To establish visual timing, use a display-appropriate timing measurement. Keep those measurements with the CSV and logbook entry.

## CLI reference

All production CLIs expose their arguments through `--help`:

```bash
python3 -m harness.soak --help
python3 -m harness.provoke --help
python3 -m harness.usb_topology --help
python3 -m harness.minimal_experiment --help
python3 -m harness.things_task --help
```
