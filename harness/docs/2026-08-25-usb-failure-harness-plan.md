# DATAPixx USB Failure Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a display-independent DATAPixx trigger soak harness, deterministic
fake hardware, controlled failure provocations, USB diagnostics, and two
source-grounded pgl visual examples.

**Architecture:** A persistent child process owns the real or simulated
`_libdpx` handle. The parent applies deadlines, writes every trigger and marker
to one CSV timeline, and replaces the worker during recovery. Visual examples
use the confirmed pgl classes directly; tests exercise pure helpers and fake
renderers without opening Metal or USB hardware.

**Tech Stack:** Python 3.9+, standard library, pytest, Pillow and NumPy already
used by pgl, pypixxlib on hardware only, macOS `system_profiler` on diagnostic
runs.

**Spec:** `harness/docs/2026-08-25-usb-failure-harness-design.md`

## Global Constraints

- Base every pgl call on a signature present at upstream commit
  `55ae664c632af828a100d8e94dd6d20832ad1ad3`.
- Do not modify any path under `pgl/`.
- Put every new source, test, and document under top-level `harness/`.
- Keep tests independent of a display, pypixxlib, and DATAPixx hardware.
- Use timezone-aware ISO 8601 wall timestamps and `time.monotonic()` values.
- Treat trigger code zero as all lines low; normal soak codes cycle from 1
  through `(2**num_bits)-1`.
- Flush each CSV row so a host or process crash loses at most the in-progress
  operation.
- Label hardware-only code paths and never claim electrical output verification
  without an external loopback or measurement device.

---

### Task 1: Mock connection, register, and trigger behavior

**Files:**

- Create: `harness/__init__.py`
- Create: `harness/mock_dpx.py`
- Create: `harness/tests/__init__.py`
- Create: `harness/tests/test_mock_dpx.py`

**Interfaces:**

- Produces: `configure(*, fail_after_calls=None, fail_after_seconds=None,
  failure_mode="error", error_code="DPX_ERR_USB", error_string="Simulated USB
  failure", persistent_handle_path=None) -> None`.
- Produces: `reset_mock(*, remove_persistent_handle=True) -> None`,
  `release_hang() -> None`, and `get_mock_state() -> dict` for tests and
  simulation-only emission verification.
- Produces every `_libdpx` symbol called by `pgl/pglVPixx.py`:
  `DPxOpen`, `DPxIsReady`, `DPxUpdateRegCache`, `DPxWriteRegCache`,
  `DPxGetTime`, `DPxGetFirmwareRev`, `DPxWriteRam`, `DPxSetDoutSchedule`,
  `DPxSetDoutSched`, `DPxSetDoutSchedRate`, `DPxStartDoutSched`, `DPxClose`,
  `DPxGetError`, `DPxGetErrorString`, `DPxClearError`, `DPxIs5VFault`,
  `DPxIsDoutPixelMode`, `DPxIsDacSchedRunning`, `DPxDisableDoutPixelMode`,
  `DPxDisableDoutPixelModeB`, `DPxDisableDoutPixelModeGB`, `DPxStopAllScheds`,
  `DPxSelectDevice`, `DPxReadProductionInfo`, `DPxGetPartNumber`,
  `DPxEnableDinDebounce`, `DPxEnableDoutButtonSchedules`,
  `DPxSetDoutButtonSchedulesMode`, `DPxSetDinLog`, `DPxStartDinLog`,
  `DPxGetDinStatus`, `DPxReadDinLog`, `DPxEnableDoutPixelMode`,
  `DPxEnableDoutPixelModeB`, `DPxEnableDoutPixelModeGB`,
  `DPxGetDoutBuffBaseAddr`, and `DPxSetDoutBuff`.
- Produces: `part_number_constants = {3: "DATAPixx3"}`.

- [ ] **Step 1: Write failing tests for connection state and stale handles**

```python
def test_open_close_and_duplicate_open(tmp_path):
    mock_dpx.configure()
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxIsReady() is True

    mock_dpx.DPxOpen()
    assert mock_dpx.DPxGetError() == "DPX_ERR_ALREADY_OPEN"

    mock_dpx.DPxClearError()
    mock_dpx.DPxClose()
    assert mock_dpx.DPxIsReady() is False


def test_persistent_handle_survives_reset_when_requested(tmp_path):
    marker = tmp_path / "driver-handle"
    mock_dpx.configure(persistent_handle_path=marker)
    mock_dpx.DPxOpen()
    assert marker.exists()

    mock_dpx.reset_mock(remove_persistent_handle=False)
    mock_dpx.configure(persistent_handle_path=marker)
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxGetError() == "DPX_ERR_ALREADY_OPEN"
```

- [ ] **Step 2: Run the connection tests and verify the missing module failure**

Run: `python3 -m pytest harness/tests/test_mock_dpx.py -k 'open or persistent' -v`

Expected: collection fails because `harness.mock_dpx` does not exist.

- [ ] **Step 3: Implement module state, open, close, and diagnostic accessors**

Use one `_MockState` dataclass guarded by an `RLock`. `DPxOpen()` creates the
optional persistent marker with `os.open(path, os.O_CREAT | os.O_EXCL |
os.O_WRONLY)`. A pre-existing in-memory or persistent handle sets
`DPX_ERR_ALREADY_OPEN`. `DPxClose()` clears readiness and unlinks a marker owned
by the current process. `DPxGetError*` and `DPxClearError()` never trigger fault
injection, so the harness can always read the simulated cause.

```python
@dataclass
class _MockState:
    opened: bool = False
    opened_at: float | None = None
    error_code: str = "DPX_SUCCESS"
    error_string: str = "Success"
    pending_ram: dict[int, list[int]] = field(default_factory=dict)
    committed_ram: dict[int, list[int]] = field(default_factory=dict)
    pending_schedule: tuple[float, int, int, int] | None = None
    schedule_started: bool = False
    emissions: list[dict[str, object]] = field(default_factory=list)
    call_count: int = 0
    persistent_handle_path: Path | None = None
    owns_persistent_handle: bool = False
```

- [ ] **Step 4: Write failing tests for staged RAM and committed emissions**

```python
def test_schedule_emits_only_when_register_cache_is_written():
    mock_dpx.configure()
    mock_dpx.DPxOpen()
    mock_dpx.DPxWriteRam(8_000_000, [7, 7, 0])
    mock_dpx.DPxSetDoutSchedule(0.0, 1000, 3, 8_000_000)
    mock_dpx.DPxStartDoutSched()
    assert mock_dpx.get_mock_state()["emissions"] == []

    mock_dpx.DPxWriteRegCache()
    emissions = mock_dpx.get_mock_state()["emissions"]
    assert len(emissions) == 1
    assert emissions[0]["samples"] == [7, 7, 0]
```

- [ ] **Step 5: Run the staged-trigger test and verify its assertion fails**

Run: `python3 -m pytest harness/tests/test_mock_dpx.py::test_schedule_emits_only_when_register_cache_is_written -v`

Expected: FAIL because schedule/RAM functions have not been implemented.

- [ ] **Step 6: Implement pending/committed state and all pgl-called wrappers**

`DPxWriteRam(address, values)` stores a copied integer list in pending RAM.
`DPxSetDoutSchedule(delay, rate, length, address)` stores the four exact values.
`DPxStartDoutSched()` stages the start. `DPxWriteRegCache()` moves pending RAM to
committed RAM and, when a start is staged, appends one emission containing the
address, rate, length, and sliced samples. Simple pgl status/configuration calls
return stable values or update booleans; DIN logging returns no events.

```python
def DPxGetFirmwareRev():
    _before_call("DPxGetFirmwareRev")
    return 42


def DPxReadProductionInfo():
    _before_call("DPxReadProductionInfo")
    return {"Assembly REV": "MOCK-A", "S/N": "MOCK-0001"}


def DPxGetDinStatus(status):
    _before_call("DPxGetDinStatus")
    status.update({"newLogFrames": 0, "currentReadFrame": 0})


def DPxReadDinLog(status, new_events):
    _before_call("DPxReadDinLog")
    return []
```

- [ ] **Step 7: Run the mock connection/trigger tests**

Run: `python3 -m pytest harness/tests/test_mock_dpx.py -k 'not failure' -v`

Expected: all selected tests pass.

- [ ] **Step 8: Commit the connection and trigger fake**

```bash
git add harness/__init__.py harness/mock_dpx.py harness/tests/__init__.py harness/tests/test_mock_dpx.py
git commit -m "test: add stateful DATAPixx fake"
```

---

### Task 2: Mock failure modes

**Files:**

- Modify: `harness/mock_dpx.py`
- Modify: `harness/tests/test_mock_dpx.py`

**Interfaces:**

- Consumes Task 1's `configure`, `_before_call`, trigger state, and inspection
  helpers.
- Produces deterministic `error`, `hang`, and `silent` behavior at call-count or
  elapsed-time thresholds.

- [ ] **Step 1: Write one failing test for each failure mode**

```python
def test_error_activates_after_exact_successful_call_count():
    mock_dpx.configure(fail_after_calls=2, error_code="DPX_ERR_TEST")
    mock_dpx.DPxOpen()
    assert mock_dpx.DPxGetFirmwareRev() == 42
    mock_dpx.DPxUpdateRegCache()
    assert mock_dpx.DPxGetError() == "DPX_SUCCESS"
    mock_dpx.DPxWriteRam(0, [1, 0])
    assert mock_dpx.DPxGetError() == "DPX_ERR_TEST"


def test_elapsed_time_failure_can_start_immediately():
    mock_dpx.configure(fail_after_seconds=0, error_code="DPX_ERR_TIMEOUT")
    mock_dpx.DPxOpen()
    mock_dpx.DPxUpdateRegCache()
    assert mock_dpx.DPxGetError() == "DPX_ERR_TIMEOUT"


def test_hang_blocks_until_test_releases_it():
    mock_dpx.configure(fail_after_calls=0, failure_mode="hang")
    mock_dpx.DPxOpen()
    thread = threading.Thread(target=mock_dpx.DPxUpdateRegCache, daemon=True)
    thread.start()
    thread.join(0.05)
    assert thread.is_alive()
    mock_dpx.release_hang()
    thread.join(0.5)
    assert not thread.is_alive()


def test_silent_failure_keeps_success_and_drops_emission():
    mock_dpx.configure(fail_after_calls=0, failure_mode="silent")
    mock_dpx.DPxOpen()
    mock_dpx.DPxWriteRam(0, [9, 0])
    mock_dpx.DPxSetDoutSchedule(0.0, 1000, 2, 0)
    mock_dpx.DPxStartDoutSched()
    mock_dpx.DPxWriteRegCache()
    assert mock_dpx.DPxGetError() == "DPX_SUCCESS"
    assert mock_dpx.DPxIsReady() is True
    assert mock_dpx.get_mock_state()["emissions"] == []
```

- [ ] **Step 2: Run the failure tests and verify behavioral failures**

Run: `python3 -m pytest harness/tests/test_mock_dpx.py -k 'failure or hang or elapsed or exact' -v`

Expected: FAIL because `configure()` does not yet activate faults.

- [ ] **Step 3: Implement a persistent fault latch in `_before_call()`**

Count faultable hardware operations after a successful `DPxOpen()`; do not
count test controls, error accessors, `DPxClearError`, or `DPxIsReady`. The first
`fail_after_calls` operations succeed and the next activates the latch. Elapsed
failure compares `time.monotonic()` with `opened_at`. Error mode sets the chosen
error; hang mode waits on an event; silent mode marks commits as dropped while
leaving ready/error state healthy.

```python
def _threshold_reached(state, now):
    calls_reached = (
        state.fail_after_calls is not None
        and state.call_count >= state.fail_after_calls
    )
    time_reached = (
        state.fail_after_seconds is not None
        and state.opened_at is not None
        and now - state.opened_at >= state.fail_after_seconds
    )
    return calls_reached or time_reached
```

- [ ] **Step 4: Run every mock test**

Run: `python3 -m pytest harness/tests/test_mock_dpx.py -v`

Expected: all mock tests pass and the hang test terminates in under one second.

- [ ] **Step 5: Commit failure injection**

```bash
git add harness/mock_dpx.py harness/tests/test_mock_dpx.py
git commit -m "feat: simulate DATAPixx USB failure modes"
```

---

### Task 3: Process-isolated device worker

**Files:**

- Create: `harness/dpx_worker.py`
- Create: `harness/tests/test_dpx_worker.py`

**Interfaces:**

- Consumes: the real `pypixxlib._libdpx` module or `harness.mock_dpx`.
- Produces: `WorkerConfig` with `simulate`, `simulation`, `num_bits`,
  `pulse_len`, `sampling_rate`, and `ram_base_address` fields.
- Produces: `DeviceWorker.start(timeout) -> dict`,
  `trigger(code, *, flush=True, timeout=None) -> dict`,
  `close(timeout=1.0) -> None`, `terminate() -> None`, and `is_alive -> bool`.
- Produces: `WorkerTimeout` and `WorkerProtocolError` exceptions.

- [ ] **Step 1: Write failing integration tests for open and trigger results**

```python
def test_simulated_worker_opens_and_reports_status():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=8))
    try:
        opened = worker.start(timeout=1.0)
        assert opened["ok"] is True
        assert opened["ready"] is True
        assert opened["firmware_revision"] == 42
        assert opened["pixel_mode"] is False
    finally:
        worker.close()


def test_trigger_reports_device_time_error_ready_and_emission():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=8))
    try:
        worker.start(timeout=1.0)
        result = worker.trigger(17, timeout=1.0)
        assert result["ok"] is True
        assert result["device_time"] >= 0
        assert result["error_code"] == "DPX_SUCCESS"
        assert result["ready"] is True
        assert result["emitted"] is True
    finally:
        worker.close()
```

- [ ] **Step 2: Run worker tests and verify the missing module failure**

Run: `python3 -m pytest harness/tests/test_dpx_worker.py -k 'opens or reports' -v`

Expected: collection fails because `harness.dpx_worker` does not exist.

- [ ] **Step 3: Implement worker import, open, condition table, and trigger path**

In the child, import the selected backend, apply simulation configuration, and
execute `DPxOpen()`, `DPxIsReady()`, `DPxUpdateRegCache()`, firmware/pixel
queries, then build the exact `setupConditions()` table. The trigger command
computes `base_address + bytes_per_condition * code`, stages the schedule,
starts it, optionally flushes, then reads time, exact error code/string, and
readiness. It clears the error only after copying it into the reply.

```python
def _condition_layout(num_bits, pulse_len, sampling_rate, base_address):
    if not 1 <= num_bits <= 24:
        raise ValueError("num_bits must be between 1 and 24")
    if base_address % 2:
        base_address += 1
    samples_per_condition = pulse_len + 1
    bytes_per_condition = samples_per_condition * 2
    table = []
    for word in range(2**num_bits):
        table.extend([word] * pulse_len)
        table.append(0)
    return base_address, samples_per_condition, bytes_per_condition, table
```

- [ ] **Step 4: Run open and trigger tests**

Run: `python3 -m pytest harness/tests/test_dpx_worker.py -k 'opens or reports' -v`

Expected: both tests pass.

- [ ] **Step 5: Write failing tests for no-flush, silent drop, error, and hang**

```python
def test_no_flush_is_reported_as_no_emission():
    worker = DeviceWorker(WorkerConfig(simulate=True, num_bits=4))
    try:
        worker.start(timeout=1.0)
        result = worker.trigger(3, flush=False, timeout=1.0)
        assert result["ok"] is False
        assert result["outcome"] == "no_emission"
    finally:
        worker.close()


def test_hung_call_times_out_and_worker_can_be_terminated():
    config = WorkerConfig(
        simulate=True,
        simulation={"fail_after_calls": 0, "failure_mode": "hang"},
    )
    worker = DeviceWorker(config)
    with pytest.raises(WorkerTimeout):
        worker.start(timeout=0.1)
    worker.terminate()
    assert worker.is_alive is False
```

Add these parallel assertions for error and silent modes:

```python
def test_error_result_preserves_exact_device_error():
    config = WorkerConfig(
        simulate=True,
        simulation={
            "fail_after_calls": 0,
            "failure_mode": "error",
            "error_code": "DPX_ERR_USB_TEST",
            "error_string": "test disconnect",
        },
    )
    worker = DeviceWorker(config)
    try:
        opened = worker.start(timeout=1.0)
        assert opened["ok"] is False
        assert opened["error_code"] == "DPX_ERR_USB_TEST"
        assert opened["error_string"] == "test disconnect"
    finally:
        worker.terminate()


def test_silent_result_is_not_mistaken_for_success():
    config = WorkerConfig(
        simulate=True,
        simulation={"fail_after_calls": 7, "failure_mode": "silent"},
        num_bits=2,
    )
    worker = DeviceWorker(config)
    try:
        worker.start(timeout=1.0)
        result = worker.trigger(1, timeout=1.0)
        assert result["ok"] is False
        assert result["ready"] is True
        assert result["error_code"] == "DPX_SUCCESS"
        assert result["emitted"] is False
        assert result["outcome"] == "no_emission"
    finally:
        worker.close()
```

- [ ] **Step 6: Run failure-path worker tests and verify failures**

Run: `python3 -m pytest harness/tests/test_dpx_worker.py -k 'flush or hung or silent or error' -v`

Expected: at least no-flush and timeout assertions fail before deadline logic
and simulation emission checks exist.

- [ ] **Step 7: Implement parent deadlines and child outcome classification**

Use `multiprocessing.get_context("spawn")`, a duplex `Pipe`, and
`connection.poll(timeout)`. On timeout raise `WorkerTimeout` without waiting for
the child. `terminate()` calls `Process.terminate()`, joins for one second, and
uses `kill()` only if the process remains alive. Mark no-flush as uncommitted on
hardware and as `no_emission` in simulation. The child closes in a `finally`
block only when it receives a normal close command or encounters a catchable
exception.

- [ ] **Step 8: Run all worker and mock tests**

Run: `python3 -m pytest harness/tests/test_mock_dpx.py harness/tests/test_dpx_worker.py -v`

Expected: all tests pass with no child-process traceback.

- [ ] **Step 9: Commit process isolation**

```bash
git add harness/dpx_worker.py harness/tests/test_dpx_worker.py
git commit -m "feat: isolate DATAPixx calls in a worker process"
```

---

### Task 4: CSV timeline and soak/recovery runner

**Files:**

- Create: `harness/csv_log.py`
- Create: `harness/soak.py`
- Create: `harness/tests/test_csv_log.py`
- Create: `harness/tests/test_soak.py`

**Interfaces:**

- Produces: `CSV_FIELDS` containing `row_type`, `wall_timestamp`,
  `monotonic_timestamp`, `device_time`, `sequence_number`, `trigger_code`,
  `error_code`, `error_string`, `ready`, `latency_ms`, `outcome`, `marker`, and
  `detail`.
- Produces: thread-safe `CsvEventLog(path)`,
  `write_trigger(*, device_time, sequence_number, trigger_code, error_code,
  error_string, ready, latency_ms, outcome, detail="") -> None`,
  `write_marker(marker, detail="", **fields) -> None`, `close() -> None`, and
  context-manager methods that close the file exactly once.
- Produces: `SoakConfig` and
  `SoakRunner(config, event_log, worker_factory=DeviceWorker)`.
- Produces CLI flags `--interval` (default `1.5`), `--reopen-interval`,
  `--call-timeout`, `--heartbeat-interval` (default `60`), `--num-bits`,
  `--pulse-len`, `--sampling-rate`, `--csv`, `--text-log`, `--max-triggers`,
  `--duration`, `--simulate`, `--simulate-fail-after-calls`,
  `--simulate-fail-after-seconds`, `--simulate-mode`,
  `--simulate-error-code`, and `--no-flush`.

- [ ] **Step 1: Write failing CSV schema and flush tests**

```python
def test_trigger_and_marker_share_one_csv_schema(tmp_path):
    path = tmp_path / "run.csv"
    log = CsvEventLog(path)
    log.write_trigger(
        device_time=1.25, sequence_number=4, trigger_code=4,
        error_code="DPX_SUCCESS", error_string="Success", ready=True,
        latency_ms=2.5, outcome="sent", detail="",
    )
    log.write_marker("kvm_switch_requested", "operator prompted")
    log.close()

    rows = list(csv.DictReader(path.open(newline="")))
    assert rows[0]["row_type"] == "trigger"
    assert rows[0]["trigger_code"] == "4"
    assert rows[1]["row_type"] == "marker"
    assert rows[1]["marker"] == "kvm_switch_requested"
    assert rows[0].keys() == rows[1].keys() == set(CSV_FIELDS)
```

- [ ] **Step 2: Run the CSV test and verify the missing module failure**

Run: `python3 -m pytest harness/tests/test_csv_log.py -v`

Expected: collection fails because `harness.csv_log` does not exist.

- [ ] **Step 3: Implement locked row writing and timezone-aware timestamps**

Open with `newline=""`, emit the header for an empty file, and under one lock
write a complete dict, call `flush()`, then `os.fsync()` so the last completed
row survives process termination. Generate wall timestamps with
`datetime.now().astimezone().isoformat(timespec="microseconds")` and monotonic
values at row creation.

- [ ] **Step 4: Run CSV tests**

Run: `python3 -m pytest harness/tests/test_csv_log.py -v`

Expected: all tests pass.

- [ ] **Step 5: Write failing soak tests with a deterministic fake worker**

```python
class ScriptedWorker:
    def __init__(self, results):
        self.results = list(results)
        self.closed = False

    def start(self, timeout):
        return {
            "ok": True, "ready": True, "error_code": "DPX_SUCCESS",
            "error_string": "Success", "firmware_revision": 42,
            "pixel_mode": False,
        }

    def trigger(self, code, flush=True, timeout=None):
        return self.results.pop(0)

    def close(self, timeout=1.0):
        self.closed = True

    def terminate(self):
        self.closed = True


def run_soak(tmp_path, config, *workers):
    worker_iter = iter(workers)
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        runner = SoakRunner(
            config, event_log,
            worker_factory=lambda worker_config: next(worker_iter),
        )
        runner.run()


def trigger_rows(path):
    with path.open(newline="") as stream:
        return [
            row for row in csv.DictReader(stream)
            if row["row_type"] == "trigger"
        ]


def test_codes_cycle_without_zero(tmp_path):
    worker = ScriptedWorker([{"ok": True, "ready": True,
                              "error_code": "DPX_SUCCESS",
                              "error_string": "Success",
                              "device_time": 1.0, "outcome": "sent"}] * 4)
    config = SoakConfig(interval=0, num_bits=2, max_triggers=4)
    run_soak(tmp_path, config, worker)
    rows = trigger_rows(tmp_path / "run.csv")
    assert [int(row["trigger_code"]) for row in rows] == [1, 2, 3, 1]
    assert [int(row["sequence_number"]) for row in rows] == [1, 2, 3, 4]


def test_failure_logs_and_reopens_before_continuing(tmp_path):
    first = ScriptedWorker([{"ok": False, "ready": False,
                             "error_code": "DPX_ERR_USB",
                             "error_string": "gone", "device_time": None,
                             "outcome": "device_error"}])
    recovered = ScriptedWorker([{"ok": True, "ready": True,
                                 "error_code": "DPX_SUCCESS",
                                 "error_string": "Success", "device_time": 2.0,
                                 "outcome": "sent"}])
    config = SoakConfig(interval=0, reopen_interval=0, max_triggers=2)
    run_soak(tmp_path, config, first, recovered)
    rows = list(csv.DictReader((tmp_path / "run.csv").open()))
    assert any(row["marker"] == "reopen_succeeded" for row in rows)
    assert [row["outcome"] for row in rows if row["row_type"] == "trigger"] == [
        "device_error", "sent"
    ]
```

The scripted test worker implements the real `start`, `trigger`, `close`, and
`terminate` interface; assertions inspect CSV behavior rather than calls on the
fake itself.

- [ ] **Step 6: Run soak tests and verify missing-runner failures**

Run: `python3 -m pytest harness/tests/test_soak.py -v`

Expected: collection fails because `SoakConfig` and `SoakRunner` do not exist.

- [ ] **Step 7: Implement cadence, heartbeat, failure logging, and recovery**

Use an absolute `next_trigger` monotonic deadline to avoid accumulating latency.
Wait through `stop_event.wait(seconds)` so SIGINT interrupts pacing and reopen
delays. Every attempt increments the sequence number once and writes one trigger
row. Any timeout, worker exception, non-success error, false readiness, or
no-emission result enters `_recover()`. Each reopen attempt writes
`reopen_attempt`, then exactly one of `reopen_failed` or `reopen_succeeded`.
Print and append heartbeat text with elapsed time and attempted-trigger count.

```python
def trigger_code(sequence_number, num_bits):
    max_code = (1 << num_bits) - 1
    return ((sequence_number - 1) % max_code) + 1
```

- [ ] **Step 8: Add a cleanup test and implement SIGINT-safe `finally`**

The test sets the runner's stop event after its first trigger and asserts the
scripted worker's real `closed` state. The CLI installs a handler that only sets
the stop event; `main()` runs the runner inside `try/finally`, closes or
terminates its worker, closes the CSV, and restores the previous signal handler.

- [ ] **Step 9: Run CSV/soak tests plus a short simulated CLI**

Run: `python3 -m pytest harness/tests/test_csv_log.py harness/tests/test_soak.py -v`

Run: `python3 -m harness.soak --simulate --interval 0.01 --max-triggers 3 --csv /tmp/pgl-soak-plan-check.csv --text-log /tmp/pgl-soak-plan-check.log`

Expected: tests pass; CLI exits zero with three sent trigger rows and a clean
close marker.

- [ ] **Step 10: Commit soak logging and recovery**

```bash
git add harness/csv_log.py harness/soak.py harness/tests/test_csv_log.py harness/tests/test_soak.py
git commit -m "feat: add resilient DATAPixx soak runner"
```

---

### Task 5: Independently selectable provocations

**Files:**

- Create: `harness/provoke.py`
- Create: `harness/tests/test_provoke.py`

**Interfaces:**

- Consumes: `SoakConfig`, `SoakRunner`, `CsvEventLog`, and `WorkerConfig`.
- Produces a required mutually exclusive mode group: `--kvm`, `--power`,
  `--bandwidth`, `--stale-handle`, `--no-flush`, or `--wiggle`.
- Produces:
  `prompt_and_mark(log, input_fn, marker_prefix, instruction) -> None`;
  `bandwidth_reader(path, stop_event, log, chunk_size=8*1024*1024) -> int`; and
  `run_stale_handle_probe(*, simulate, persistent_handle_path, event_log,
  timeout) -> dict`.

- [ ] **Step 1: Write failing parser and prompt-marker tests**

```python
def test_exactly_one_provocation_is_required():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(SystemExit):
        parser.parse_args(["--kvm", "--power"])
    assert parser.parse_args(["--kvm"]).kvm is True


def test_kvm_prompt_marks_before_and_after_action(tmp_path):
    answers = iter(["", ""])
    path = tmp_path / "run.csv"
    with CsvEventLog(path) as log:
        prompt_and_mark(
            log, lambda _: next(answers), "kvm_switch",
            "Switch the KVM once, then return to this terminal.",
        )
    rows = list(csv.DictReader(path.open()))
    assert [row["marker"] for row in rows] == [
        "kvm_switch_requested", "kvm_switch_completed"
    ]
```

- [ ] **Step 2: Run provocation tests and verify the missing module failure**

Run: `python3 -m pytest harness/tests/test_provoke.py -k 'required or kvm' -v`

Expected: collection fails because `harness.provoke` does not exist.

- [ ] **Step 3: Implement the mutually exclusive parser and human prompts**

KVM and power each use before/after Enter prompts. In power mode, the operator
plugs in one designated bus-powered device on the documented shared USB bus
mid-run and leaves the DATAPixx powered and untouched. Wiggle calls the same
helper in this fixed order: Mac connector, DATAPixx connector, MSR feedthrough.
Every instruction names the one physical action to perform. `--kvm` appears
first in CLI help and README examples.

- [ ] **Step 4: Write failing bandwidth and stale-handle tests**

```python
def test_bandwidth_reader_rewinds_existing_file_and_marks_bytes(tmp_path):
    class StopAfterReads:
        def __init__(self, allowed_reads):
            self.allowed_reads = allowed_reads
            self.checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks > self.allowed_reads

    source = tmp_path / "traffic.bin"
    source.write_bytes(b"abcdefgh")
    stop = StopAfterReads(3)
    with CsvEventLog(tmp_path / "run.csv") as log:
        total = bandwidth_reader(source, stop, log, chunk_size=4)
    assert total == 12
    assert source.read_bytes() == b"abcdefgh"


def test_stale_handle_probe_records_reopen_refusal(tmp_path):
    with CsvEventLog(tmp_path / "run.csv") as event_log:
        result = run_stale_handle_probe(
            simulate=True,
            persistent_handle_path=tmp_path / "mock-handle",
            event_log=event_log,
            timeout=1.0,
        )
    assert result["ok"] is False
    assert result["error_code"] == "DPX_ERR_ALREADY_OPEN"
```

- [ ] **Step 5: Run bandwidth/stale tests and verify failures**

Run: `python3 -m pytest harness/tests/test_provoke.py -k 'bandwidth or stale' -v`

Expected: FAIL because file traffic and forced child exit are not implemented.

- [ ] **Step 6: Implement read-only bandwidth traffic and stale child exit**

Bandwidth requires `--bandwidth-source`; validate it is a regular readable file.
Read fixed chunks in a loop and seek to zero at EOF. Mark start, each 1 GiB
milestone, and stop. Never open the file for writing.

The stale helper imports the selected backend, configures the simulation's
persistent marker if requested, calls `DPxOpen()`, sends an opened acknowledgment
to the parent, and calls `os._exit(0)` without `DPxClose()`. A fresh
`DeviceWorker` then attempts to start immediately; log the exact reopen result.

- [ ] **Step 7: Integrate each mode with the soak runner**

Open one `CsvEventLog`, pass it to `SoakRunner`, and run the soak in a background
thread while the main thread handles input and signals. `--no-flush` sets only
`SoakConfig.flush_triggers=False`. Human and bandwidth modes set no other soak
field. Stale-handle runs its probe before the soak open attempt so the first
recovery result captures whether a new process can reclaim the device.

- [ ] **Step 8: Run all provocation tests**

Run: `python3 -m pytest harness/tests/test_provoke.py -v`

Expected: all tests pass without changing the source bandwidth file.

- [ ] **Step 9: Commit provocations**

```bash
git add harness/provoke.py harness/tests/test_provoke.py
git commit -m "feat: add controlled USB failure provocations"
```

---

### Task 6: USB topology parser and macOS command

**Files:**

- Create: `harness/usb_topology.py`
- Create: `harness/tests/fixtures/system_profiler_usb.json`
- Create: `harness/tests/test_usb_topology.py`

**Interfaces:**

- Produces: `parse_current_ma(value) -> int | None`,
  `walk_usb_tree(profile) -> list[UsbNode]`, `format_usb_tree(nodes) -> str`, and
  `collect_usb_profile() -> dict`.
- CLI accepts `--json-output PATH` and `--input-json PATH`; the latter enables
  reproducible offline parsing.

- [ ] **Step 1: Add a fixed nested profile and failing current-parser tests**

The fixture contains a controller with `current_available: "500 mA"`, a hub,
one 100 mA child, and one 900 mA child. Use realistic `_name`, `_items`,
`vendor_id`, `product_id`, `location_id`, `current_available`, and
`current_required` keys.

```python
@pytest.fixture
def fixture_profile():
    path = Path("harness/tests/fixtures/system_profiler_usb.json")
    return json.loads(path.read_text())


@pytest.mark.parametrize(
    ("value", "expected"),
    [("500 mA", 500), ("1,500 mA", 1500), (500, 500), (None, None), ("", None)],
)
def test_parse_current_ma(value, expected):
    assert parse_current_ma(value) == expected


def test_tree_preserves_hierarchy_and_flags_overdraw(fixture_profile):
    rendered = format_usb_tree(walk_usb_tree(fixture_profile))
    assert "USB Controller" in rendered
    assert "  Hub" in rendered
    assert "    Camera" in rendered
    assert "OVER CURRENT: requires 900 mA, parent offers 500 mA" in rendered
```

- [ ] **Step 2: Run topology tests and verify the missing module failure**

Run: `python3 -m pytest harness/tests/test_usb_topology.py -v`

Expected: collection fails because `harness.usb_topology` does not exist.

- [ ] **Step 3: Implement normalized node traversal and nearest-parent limits**

Normalize both snake-case JSON keys and display labels such as `Current
Available (mA)`. Each `UsbNode` stores name, depth, current values, nearest
ancestor available-current limit, identifiers, and over-current boolean. A hub's
available current becomes the inherited limit for its descendants; otherwise
the nearest ancestor's limit continues.

- [ ] **Step 4: Implement command execution and raw JSON dump**

Run exactly `system_profiler SPUSBDataType -json` with
`subprocess.run(["system_profiler", "SPUSBDataType", "-json"], check=True,
capture_output=True, text=True)`, parse stdout, and raise a concise
runtime error on missing command, nonzero exit, or invalid JSON. `--json-output`
writes the unmodified parsed structure with indentation. `--input-json` skips
the subprocess and works on any OS.

- [ ] **Step 5: Run topology tests and offline CLI**

Run: `python3 -m pytest harness/tests/test_usb_topology.py -v`

Run: `python3 -m harness.usb_topology --input-json harness/tests/fixtures/system_profiler_usb.json`

Expected: tests pass; CLI shows the nested tree and one over-current warning.

- [ ] **Step 6: Commit diagnostics**

```bash
git add harness/usb_topology.py harness/tests/fixtures/system_profiler_usb.json harness/tests/test_usb_topology.py
git commit -m "feat: report macOS USB topology and power limits"
```

---

### Task 7: Minimal pgl chain and preloaded ThingsTask

**Files:**

- Create: `harness/minimal_experiment.py`
- Create: `harness/things_task.py`
- Create: `harness/tests/test_things_task.py`

**Interfaces:**

- Consumes only confirmed pgl signatures:
  `pgl()`, `pglExperiment(pgl=pgl_instance,
  experimentName="usbFailureMinimal", subjectID="s0000")`,
  `initScreen()`, `endScreen()`, `pglDataPixx()`, `isActive`,
  `setupConditions(numBits=8, pulseLen=3)`, `writeCondition(code)`,
  `closeDPx()`, `imageCreate(image_data)`, `pglImageInstance.display(height=18)`,
  `flush()`, `waitSecs(seconds)`, `pglTask.__init__(pgl)`, `startSegment()`, and
  `updateScreen()`.
- Produces: `ThingsTask(pgl_instance, data_pixx, image_dir, image_count=200,
  image_height=18)` with fixed `[0.5, 1.0]` segments and 200 trials.
- `minimal_experiment.py` accepts `--image` and defaults to repository
  `testimage.jpg`; `things_task.py` accepts required `--image-dir`.

- [ ] **Step 1: Re-grep every planned pgl call immediately before coding**

Run:

```bash
rg -n "def (initScreen|endScreen|setupConditions|writeCondition|closeDPx|imageCreate|display|flush|waitSecs|startSegment|updateScreen|rect)" pgl tutorials
```

Expected: every call and positional/keyword signature used below appears in the
listed ground-truth files. Remove any call whose signature is not confirmed and
record the missing capability in the README.

- [ ] **Step 2: Write failing preload and segment-trigger tests**

```python
class FakeTexture:
    def __init__(self):
        self.display_heights = []

    def display(self, height=None):
        self.display_heights.append(height)


class FakeRenderer:
    def __init__(self):
        self.created_images = []
        self.textures = []

    def imageCreate(self, image_data):
        self.created_images.append(image_data.copy())
        texture = FakeTexture()
        self.textures.append(texture)
        return texture

    def rect(self, x, y, width, height, color):
        return None


class FakeDataPixx:
    def __init__(self):
        self.conditions = []

    def writeCondition(self, condition):
        self.conditions.append(condition)


@pytest.fixture
def tiny_image_factory():
    def make(path, index):
        Image.new("RGB", (2, 2), color=(index % 256, 0, 0)).save(path)
    return make


def test_constructor_creates_all_200_textures(tmp_path, tiny_image_factory):
    for index in range(200):
        tiny_image_factory(tmp_path / f"image-{index:03d}.png", index)
    renderer = FakeRenderer()
    task = ThingsTask(renderer, FakeDataPixx(), tmp_path)
    assert len(task.textures) == 200
    assert len(renderer.created_images) == 200
    assert task.settings.seglen == [0.5, 1.0]
    assert task.settings.nTrials == 200


@pytest.fixture
def preloaded_task(tmp_path, tiny_image_factory):
    for index in range(200):
        tiny_image_factory(tmp_path / f"image-{index:03d}.png", index)
    return ThingsTask(FakeRenderer(), FakeDataPixx(), tmp_path)


def test_stimulus_segment_sends_one_based_trial_code(preloaded_task):
    preloaded_task.state.currentTrial = 0
    preloaded_task.state.currentSegment = 0
    preloaded_task.startSegment(12.5)
    assert preloaded_task.data_pixx.conditions == [1]


def test_fixation_segment_does_not_send_trigger(preloaded_task):
    preloaded_task.state.currentTrial = 0
    preloaded_task.state.currentSegment = 1
    preloaded_task.startSegment(13.0)
    assert preloaded_task.data_pixx.conditions == []
```

- [ ] **Step 3: Run task tests and verify missing-class failures**

Run: `python3 -m pytest harness/tests/test_things_task.py -k 'constructor or segment' -v`

Expected: collection fails because `ThingsTask` does not exist.

- [ ] **Step 4: Implement sorted discovery and constructor-only texture preload**

Accept Pillow-supported `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, and
`.webp` files. Sort by filename, require at least `image_count`, open each chosen
file in a context manager, convert it to RGB, copy it into a NumPy array, and
call `self.pgl.imageCreate(array)`. Raise if creation returns `None`. Store only
paths and Metal texture objects after construction.

```python
self.settings.taskName = "THINGS USB Trigger Task"
self.settings.seglen = [0.5, 1.0]
self.settings.nTrials = image_count
self.settings.fixedParameters = {
    "imageDirectory": str(Path(image_dir).resolve()),
    "imageCount": image_count,
    "imageHeight": image_height,
}
```

- [ ] **Step 5: Implement start and draw callbacks**

`startSegment()` sends `self.state.currentTrial + 1` only for segment zero.
`updateScreen()` indexes the pre-created texture only during segment zero and
draws the confirmed fixation primitive during both segments. Do not call
`Image.open`, `np.asarray`, `pgl.imageCreate`, or any database `get()` method in
either callback.

- [ ] **Step 6: Write and run a test that forbids decoding during screen updates**

After constructing the task, monkeypatch `PIL.Image.open` to raise, invoke
`updateScreen()` in both segments, and assert the preloaded texture displayed
only in segment zero. This fails if future code moves decoding onto the frame
path.

```python
def test_update_screen_uses_preloaded_texture_without_decoding(
    preloaded_task, monkeypatch
):
    def fail_if_opened(*args, **kwargs):
        raise AssertionError("image decoding occurred during updateScreen")

    monkeypatch.setattr(Image, "open", fail_if_opened)
    preloaded_task.state.currentTrial = 0
    preloaded_task.state.currentSegment = 0
    preloaded_task.updateScreen()
    preloaded_task.state.currentSegment = 1
    preloaded_task.updateScreen()
    assert preloaded_task.textures[0].display_heights == [18]
```

Run: `python3 -m pytest harness/tests/test_things_task.py -v`

Expected: all task tests pass.

- [ ] **Step 7: Implement the real-hardware-only minimal script**

Create screen, DATAPixx, and texture variables as `None`; then execute the six
required actions in order. Verify `data_pixx.isActive`, call
`setupConditions(numBits=8)`, print direct firmware and pixel-mode queries from
`data_pixx.dp`, send known codes 17 and 18, display/flush/wait 0.5 seconds, and
close DATAPixx before `experiment.endScreen()` in nested `finally` cleanup.
Guard main with `if __name__ == "__main__"` and print a hardware-only warning
before opening anything.

- [ ] **Step 8: Implement the real-hardware ThingsTask runner**

Follow the tutorial sequence: create pgl, create experiment, call `initScreen()`,
create and validate `pglDataPixx`, call `setupConditions(numBits=8)`, create the
task so textures preload while the screen is open, add device and task, then
call `experiment.run()`. Close DATAPixx and screen in `finally`. Do not add an
invented experiment lifecycle hook.

- [ ] **Step 9: Compile visual scripts without running hardware paths**

Run: `python3 -m py_compile harness/minimal_experiment.py harness/things_task.py`

Expected: exit zero. Do not invoke either script's `main()` in automated tests.

- [ ] **Step 10: Commit visual examples**

```bash
git add harness/minimal_experiment.py harness/things_task.py harness/tests/test_things_task.py
git commit -m "feat: add minimal pgl and preloaded THINGS experiments"
```

---

### Task 8: Operating guide, constraints audit, and full verification

**Files:**

- Create: `harness/README.md`
- Modify tests only if verification reveals an uncovered behavior; use a fresh
  red-green cycle for each correction.

**Interfaces:**

- Documents the upstream SHA, setup, every CLI, CSV schema, OS USB logging,
  safety warning, hypothesis table, logbook template, onset-trigger finding,
  and hardware verification limits.

- [ ] **Step 1: Draft the README around copyable commands**

Include commands for:

```bash
python3 -m harness.soak --simulate --interval 0.1 --max-triggers 10
python3 -m harness.soak --csv logs/baseline.csv --text-log logs/baseline.log
python3 -m harness.provoke --kvm --csv logs/kvm.csv
python3 -m harness.provoke --power --csv logs/power.csv
python3 -m harness.provoke --bandwidth --bandwidth-source /Volumes/USB/large-file.bin --csv logs/bandwidth.csv
python3 -m harness.provoke --stale-handle --csv logs/stale.csv
python3 -m harness.provoke --no-flush --simulate --max-triggers 1 --csv logs/no-flush.csv
python3 -m harness.provoke --wiggle --csv logs/wiggle.csv
python3 -m harness.usb_topology --json-output logs/usb-topology.json
python3 -m harness.minimal_experiment --image testimage.jpg
python3 -m harness.things_task --image-dir /path/to/things/images
log stream --style compact --predicate 'subsystem == "com.apple.iokit.IOUSBHostFamily"'
```

- [ ] **Step 2: Add the hypothesis table and logbook template**

Cover KVM re-enumeration, aggregate bandwidth, bus power, stale software handle,
missing register flush, cable/connector intermittency, and host-controller/hub
topology. Each row states one test and a result that confirms or weakens the
hypothesis. The logbook includes date/time, CSV/log paths, topology snapshot,
software/firmware revisions, trigger interval, elapsed time, failure/recovery,
and a required “what single variable changed” column.

- [ ] **Step 3: Add explicit safety and onset findings**

Place this warning before the first hardware command: “Run soak and provocation
tests with no participant in the helmet or scanner room.” State that
`pglExperiment.run()` sends no digital output when the start key is accepted;
`startPhase(0)` may invoke a task trigger, but there is no distinct onset code.
Recommend reserving an explicit experiment-onset condition in the lab task.

- [ ] **Step 4: Document what simulation and hardware can prove**

Simulation verifies error, hang, silent-drop, no-flush, stale-handle, logging,
and recovery control paths. On hardware, ready/error checks prove only library
and register communication. Confirming a voltage pulse requires MEG input,
loopback, logic analyzer, or oscilloscope. List macOS/Metal, DATAPixx firmware,
USB electrical behavior, KVM behavior, and physical recovery as unverified in
this development environment.

- [ ] **Step 5: Run the complete automated verification**

Run:

```bash
python3 -m pytest harness/tests -v
python3 -m compileall -q harness
git diff upstream/main --check
git diff --name-only upstream/main -- pgl
```

Expected: all tests pass; compileall and diff check exit zero; the final command
prints nothing.

- [ ] **Step 6: Exercise deterministic CLI flows**

Use a temporary directory and run a successful simulated soak, an injected
error/recovery soak that stops after a finite duration, a one-trigger no-flush
control, and offline topology rendering. Inspect each CSV with `csv.DictReader`
and assert expected trigger/marker outcomes rather than checking source text.

- [ ] **Step 7: Audit every requested deliverable against the README and diff**

Check Deliverables 0–4 line by line, verify the upstream SHA, confirm no
participant warning and onset finding, and list every item requiring real
hardware. Fix a discovered implementation gap only after adding a test that
fails for that gap.

- [ ] **Step 8: Commit documentation and final test refinements**

```bash
git add harness/README.md harness/tests harness/*.py
git commit -m "docs: add DATAPixx failure-harness runbook"
```

- [ ] **Step 9: Invoke verification-before-completion and requesting-code-review**

Re-run the exact full verification commands after the final commit. Review the
committed diff against this plan and the design; resolve every blocking finding
with a red-green test cycle and a focused follow-up commit.

- [ ] **Step 10: Push and open the fork PR**

```bash
git push -u origin usb-failure-harness
gh pr create --repo lobennett/pgl --base main --head usb-failure-harness --title "Add DATAPixx USB failure reproduction harness" --body-file /tmp/pgl-usb-failure-pr.md
```

The PR body summarizes the mock, soak/recovery path, provocations, diagnostics,
visual examples, automated verification, onset-trigger finding, and the exact
real-hardware checks still outstanding. Do not target `justingardner/pgl`.
