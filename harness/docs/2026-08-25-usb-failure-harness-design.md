# DATAPixx USB Failure Harness Design

## Purpose

This harness turns an intermittent DATAPixx3 USB failure into a controlled,
timestamped experiment. It exercises the same digital-output sequence that pgl
uses, detects errors and hangs, retries the connection, and places human and USB
events on the same timeline. None of its automated tests require a display,
pypixxlib, or DATAPixx hardware.

The work is based on upstream commit
`55ae664c632af828a100d8e94dd6d20832ad1ad3`.

## Source-grounded boundaries

The visual programs use only calls confirmed in these sources:

- `pgl/pglExperiment.py`: `pglExperiment.initScreen()`, `run()`, `endScreen()`,
  and the `pglTask` lifecycle.
- `pgl/pglVPixx.py`: `pglDataPixx`, `openDPx()`, `closeDPx()`,
  `setupConditions()`, and `writeCondition()`.
- `pgl/pglDevice.py`: the `pglDigitalIODevice` interface.
- `tutorials/experiment.ipynb`, `tutorials/vpixx.ipynb`, and
  `tutorials/images.ipynb`: task subclassing, trigger setup, and image texture
  creation.

The display-free programs use the low-level sequence implemented by
`pglDataPixx.writeCondition()`:

1. `DPxSetDoutSchedule(...)`
2. `DPxStartDoutSched()`
3. `DPxWriteRegCache()`

The harness does not change any file under `pgl/`.

## Architecture

### Mock module

`harness/mock_dpx.py` implements the `_libdpx` functions called anywhere in
`pgl/pglVPixx.py`. Its module-level API makes it usable in place of
`pypixxlib._libdpx`.

The mock keeps pending register and RAM operations separate from committed
operations. `DPxWriteRegCache()` commits a scheduled trigger; omitting it leaves
the trigger pending and emits nothing. Test-only inspection functions expose
committed emissions without changing the hardware path.

The mock supports these deterministic failure controls:

- fail after a chosen number of successful calls;
- fail after a chosen interval from `DPxOpen()`;
- set a chosen DATAPixx error code and string;
- block indefinitely to model a hung USB call;
- remain ready and error-free while silently dropping emitted triggers; and
- reject `DPxOpen()` while an earlier handle remains open.

An optional state file preserves an open-handle marker across processes for the
stale-kernel provocation. Normal simulations keep state in memory so a killed
hung worker can recover.

### Device worker

`harness/dpx_worker.py` owns the pypixxlib handle in a child process. It imports
either the real `_libdpx` module or `mock_dpx`, opens the device, builds the
condition table using the same layout as `pglDataPixx.setupConditions()`, and
handles trigger and diagnostic commands.

The controller sends commands over a multiprocessing pipe. Each command has a
deadline. If the worker stops responding, the controller records a hang,
terminates the worker, and starts the reopen loop. This keeps the CSV logger and
SIGINT handler responsive even when a USB library call never returns.

### Soak runner and CSV

`harness/soak.py` owns scheduling, logging, and recovery. It sends one-based
trigger codes, wrapping at the configured bit width, and records a monotonic
sequence number so a missing trigger remains visible across code wraparound.
The default interval is 1.5 seconds.

Every attempted trigger produces one CSV row. Marker and recovery events use
the same schema:

- row type;
- timezone-aware wall-clock timestamp;
- monotonic timestamp;
- DATAPixx device time;
- sequence number and trigger code;
- exact error code and error string;
- readiness;
- round-trip call latency;
- outcome; and
- marker/detail text.

The writer flushes every row. A heartbeat is also written to the text log every
60 seconds. On failure, the runner records the exact outcome, stops or kills the
worker, and retries open at a configurable interval until it succeeds or the
operator stops the run.

The runner always closes a responsive worker in `finally`. SIGINT sets the stop
event and reaches that cleanup path.

### Provocations

`harness/provoke.py` wraps the soak runner and changes only the mode selected by
the operator:

- KVM and cable-wiggle modes write markers before and after each prompted
  action. Power mode instead prompts the operator to plug in one designated
  bus-powered device on the documented shared USB bus mid-run, while leaving
  the DATAPixx powered and untouched; it records the same before/after markers.
- Bandwidth mode repeatedly reads an existing large file supplied through
  `--bandwidth-source`. It creates or deletes no files on the USB volume.
- Stale-handle mode opens the device in a helper process, exits it without
  `DPxClose()`, and immediately attempts to open from a new worker.
- No-flush mode omits `DPxWriteRegCache()`. Simulation verifies from the mock's
  committed-emission count that nothing was emitted. Real hardware records the
  result as deliberately uncommitted and electrically unverified.

Each mode writes its markers into the soak CSV.

### Visual programs

`harness/minimal_experiment.py` performs one linear hardware check: open the
screen, construct `pglDataPixx` (which calls `openDPx()` and prints firmware and
pixel-mode state), prepare condition codes, send the first code, create and show
one image texture for 500 ms, send the second code, then close the DATAPixx and
screen in `finally` blocks.

`harness/things_task.py` defines a two-segment `pglTask`: a 0.5-second image
segment and a 1.0-second fixation segment. Its constructor requires at least
200 supported image files, sorts them, decodes the first 200, and creates all
200 Metal textures. `updateScreen()` only displays a pre-created texture and
draws fixation; it performs no file I/O or decoding. `startSegment()` sends the
one-based trial code when segment zero begins.

### USB topology

`harness/usb_topology.py` invokes
`system_profiler SPUSBDataType -json`, optionally saves the raw result, and
prints the nested `_items` tree. It parses current values expressed in mA and
flags a child whose `Current Required` exceeds the nearest parent hub's
`Current Available`. Parsing is separate from command execution so Linux-safe
fixtures can test the hierarchy and current checks.

## Error semantics

A trigger succeeds only when the worker replies before its deadline, the
device reports ready, and `DPxGetError()` reports `DPX_SUCCESS`. In simulation,
the committed-emission count must also advance. Silent drops and no-flush calls
therefore fail even though their error and ready checks look healthy.

Real pypixxlib exposes no electrical output readback in the pgl path inspected
for this work. The harness can prove that the library accepted and flushed a
schedule; it cannot prove that voltage appeared on the output connector. The
README will distinguish those claims and recommend a loopback or oscilloscope
when electrical confirmation matters.

## Experiment-onset finding

`pglExperiment.run()` sends no digital output when the start key is accepted.
It sets `state.experimentStarted`, calls `startPhase(0)`, prints its status, and
then records `data.startTime`. A task may send a trigger indirectly when
`startPhase()` calls `task.start()`, but pgl emits no distinct experiment-onset
code. The README will call this out because continuously running MEG acquisition
needs an explicit synchronization event.

## Test strategy

Pytest tests will run against real harness code and the fake module. They will
cover:

- all mock failure thresholds and modes;
- staged versus committed trigger state;
- stale-handle rejection and cleanup;
- command timeout and worker termination;
- exact trigger, error, readiness, latency, and marker CSV fields;
- recovery after injected failures;
- no-flush and silent-drop detection;
- SIGINT-style cleanup through the runner stop path;
- ThingsTask's 200-texture preload and display-only update path using small
  temporary images and a fake renderer; and
- USB hierarchy/current parsing from fixed system-profiler JSON.

The final verification will run the full pytest suite, compile every Python file
under `harness/`, inspect the diff for changes under `pgl/`, and exercise the
simulate CLIs with short deterministic runs.
