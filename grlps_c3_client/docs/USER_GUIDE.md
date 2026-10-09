# GRL C3 Python Client — User Guide

Every method, what it takes, and what it returns.

The README covers installing and the first run. This guide is the reference you come back to.

---

## Contents

**[Getting started](#getting-started)**

- [Runtime requirements](#runtime-requirements)
- [The two ways of working](#the-two-ways-of-working)
- [The workspace](#the-workspace)
- [Upgrading a workspace](#upgrading-a-workspace)
- [Configuration](#configuration)

**[The API](#the-api)**

- [Call order](#call-order)
- [`GRLApiClient(config_file_path, app, ip_address)`](#grlapiclientconfig_file_path-app-ip_address)
- [`with GRLApiClient(...) as client`](#with-grlapiclient-as-client)
- [`launch_app()`](#launch_app)
- [`connect(ip_address=None)`](#connectip_addressnone)
- [`run_compliance(project_name, esdf, test_cases)`](#run_complianceproject_name-esdf-test_cases)
- [`run_exerciser(sequence_file, dry_run, hold)`](#run_exercisersequence_file-dry_run-hold)
- [`disconnect()`](#disconnect)
- [`get_telemetry()`](#get_telemetry)
- [`call_api(api_name, ...)`](#call_apiapi_name-)

**[Reference](#reference)**

- [The shipped scripts](#the-shipped-scripts)
- [What a run writes](#what-a-run-writes)
- [Troubleshooting](#troubleshooting)

---

## Getting started

### Runtime requirements

| | |
|---|---|
| OS | Windows |
| Python | 3.11 or newer, enforced at import |
| Software | the GRL C3 application, installed and licensed |
| Hardware | a reachable GRL C3 tester |

One install drives four applications:

| Application | Emulates | Typical port |
|---|---|:--:|
| `GRL-C3-MP-TPR` | a receiver, to test a transmitter | 2002 |
| `GRL-C3-TPT-MPP` | a transmitter, to test a receiver | 2004 |
| `GRL-WP-TPR-C3` | a receiver, to test a transmitter | 3003 |
| `GRL-C3-TPT-BPP-EPP` | a transmitter, in BPP/EPP firmware | 2004 |

REST and WebSocket builds are detected automatically. There is nothing to configure, and no
separate install for either.

---

### The two ways of working

**Configuration-based.** Put everything in `grl_config.json` and call each step with no arguments.
This is what the commands do.

**Script-based.** Pass what you want in the script. Anything you pass is used for that run;
anything you leave out falls back to the configuration file. Nothing is written back, so a script
that supplies its own values never disturbs the configured ones.

| Input | In the script | Falls back to |
|---|---|---|
| Application | `GRLApiClient(app=...)` | `Selected_app` |
| Tester address | `GRLApiClient(ip_address=...)` or `connect(ip)` | that application's `ip_address` |
| Project name | `run_compliance(project_name=...)` | `ProjectConfigurationModel` |
| Description file | `run_compliance(esdf=...)` | `files.EsdfConfigurationModel` |
| Test cases | `run_compliance(test_cases=[...])`, or `["ALL"]` | `Manual_test_cases.json` |
| Exerciser sequence | `run_exerciser(sequence_file=...)` | `files.ExerciserSequenceModel` |
| Dry run | `run_exerciser(dry_run=...)` | `common.exerciser.dry_run` |
| Session length | `run_exerciser(hold=...)` | until Enter is pressed |

Test cases follow one rule wherever they come from: names run the ones the description file allows,
`["ALL"]` runs every case it allows, and an empty or unreadable selection runs nothing.

| `Manual_test_cases.json` holds | What happens |
|---|---|
| case names | the ones the description file allows run; any it does not allow are named and skipped |
| `["ALL"]` | every case the description file allows runs |
| `[]`, nothing, or no file | nothing runs, and the message says which file to edit |
| invalid JSON, or not a list | nothing runs, and the message says what is wrong |
| `"ALL"` together with names | nothing runs — `"ALL"` must be the only entry |

---

### The workspace

Everything lives in the directory where you ran `c3-init`:

```
your-bench-folder\
├── grl_config.json                      applications, addresses, file choices, run settings
├── JSON_User_input\
│   └── <application>\                   one folder per application
│       ├── esdf\                        put your description files here
│       ├── exerciser\                   one exerciser sequence per power profile
│       ├── project_config.json
│       ├── tester_config.json
│       ├── report_config.json
│       └── OptimumCoilValue*            optimum coil values
├── Test_Case_List_From_System\
│   └── <application>\
│       ├── Received_test_cases.json     generated: every applicable case
│       ├── Manual_test_cases.json       yours: which of them to run, or ["ALL"]
│       └── selected_test_cases.json     generated: what was actually selected
├── Run_time_files\<application>\        generated: what the application reported back
├── Runtime_Capture\<application>\       generated: the application's own reports, per run
└── logs\                                generated: one log and one HTTP trace per run
```

One workspace holds all four applications side by side. A second workspace is only needed when you
want a second set of settings.

To run the commands from somewhere else, point them at the folder:

```powershell
set GRL_C3_PROJECT_ROOT=C:\benches\my-bench
```

| Variable | Effect |
|---|---|
| `GRL_C3_PROJECT_ROOT` | use this folder as the workspace, wherever you run from |
| `GRL_C3_HOME` | where `c3-init` creates the workspace, instead of the current directory |

Importing the library never creates files — only `c3-init` writes anything.

---

### Upgrading a workspace

`c3-init` never overwrites what you have set, so run it again after upgrading the package: that is
how settings a new version adds reach a workspace that already exists.

| Run | Does |
|---|---|
| `c3-init` in an empty folder | creates the workspace and seeds it |
| `c3-init` in an existing one | merges in settings new to this version, restores missing files, reports both, and changes nothing you have set |
| `c3-init` in a workspace from an earlier version | also moves exerciser sequences that sit loose beside the configuration files into `exerciser\`, and updates a setting that names a moved file, after backing the configuration up |
| `c3-init --tester ADDRESS` | writes the tester address for `Selected_app`, or for `--app NAME`; nothing else changes |
| `c3-init --force` | replaces the configuration and the shipped example inputs with this version's defaults, after copying your configuration to `grl_config.json.bak-<timestamp>` |

The merge adds keys at any depth — a new run control inside `common.exerciser`, a whole new
application under `applications`. A key that is already there is left alone whatever its value, and
a list you have edited is left whole rather than merged entry by entry, because a list is a value
and not structure.

Files you added yourself — your description files, your exported sequences — are never touched, not
even by `--force`, which only replaces the files the package ships.

### Configuration

`grl_config.json` has three parts.

**`Selected_app`** — which application the commands drive when `--app` is not given.

**`applications`** — one block per application:

| Key | Meaning |
|---|---|
| `app_name` | must match the block's own name |
| `app_path` | the installed executable |
| `known_port` | the port the application serves on |
| `ip_address` | your tester. Ships as `192.0.2.50`, an unroutable placeholder; `c3-init --tester` sets it |
| `is_multiple_esdf_files` | run once per description file in the `esdf` folder |
| `files.EsdfConfigurationModel` | the description file, relative to this application's folder |
| `files.ExerciserSequenceModel` | the exerciser sequence, e.g. `exerciser/ExerciserSequence-MPP25.json`. A bare file name is also looked for in `exerciser\` |
| `files.OptimumCoilValue` | optimum coil values, a JSON or XML file. `OptimumCoilValueXml` is the older name for the same setting and is still read |
| `files.ProjectConfigurationModel` / `TesterConfigurationModel` / `ReportConfigurationModel` | the three setup files |

**`common`** — how a run behaves:

| Key | Meaning |
|---|---|
| `run_mode` | `compliance` or `exerciser`. The commands state their own mode and ignore this; `sample_run.main()` uses it when no mode is passed |
| `exerciser.steps` | which settings to apply, in order |
| `exerciser.verify` | `strict`, `warn` or `off` |
| `exerciser.dry_run` | compose every request and send none |
| `exerciser.stop_on_error` | stop the session at the first failed step |
| `exerciser.channels` | which live measurements to record |
| `trace_http` | write an HTTP trace beside the log |

---

## The API

### Call order

```
GRLApiClient(...)        build the client
  launch_app()           start the application and pick the transport
  connect()              connect the tester; reads the licence
  run_compliance(...)    compliance: project, description file, cases, run, report
    or
  run_exerciser(...)     exerciser: apply the exported settings, start, hold, stop, capture
  disconnect()           always, in a finally
```

`launch_app()` must come first: until the application is up, nothing knows which port to talk to or
whether this is a REST or a WebSocket build. `connect()` must come before either run method — the
exerciser licence is read from the connection.

`with GRLApiClient(...) as client:` does the first two steps on entry and the last on exit, so the
block only holds the run itself.

---

### `GRLApiClient(config_file_path, app, ip_address)`

Builds the client. Starts nothing.

| Parameter | Default | Meaning |
|---|---|---|
| `config_file_path` | `"grl_config.json"` | the configuration file. Relative paths resolve against the current directory |
| `app` | `None` | which application to drive. `None` uses `Selected_app` |
| `ip_address` | `None` | the tester. `None` uses that application's configured address |

```python
from grlps_c3_client import GRLApiClient

client = GRLApiClient()                                   # config file, Selected_app
client = GRLApiClient(app="GRL-WP-TPR-C3")                # a named application
client = GRLApiClient(app="GRL-C3-MP-TPR", ip_address="192.0.2.77")   # one script, two benches
```

**Raises** `FileNotFoundError` if the configuration file is not there, naming the path it looked at
and the directory it looked from — the usual cause is a relative path and the wrong working
directory. **Raises** `ValueError` for an unknown `app`, listing the configured ones.

---

### `with GRLApiClient(...) as client`

Starts the application and connects the tester on entry; disconnects and closes the application on
exit — normally or through an exception.

```python
with GRLApiClient(app="GRL-C3-MP-TPR", ip_address="192.0.2.77") as client:
    print(client.run_compliance(test_cases=["ALL"]))
```

**Raises** `RuntimeError` on entry if the application does not start or the tester does not
connect, saying which and why. Everything is closed again first, and the block does not run.

---

### `launch_app()`

Starts the application if it is not already running, waits for its port, reads its software version
and decides the transport.

**Input:** none. Uses `app_path` and `known_port`.

**Returns** `True` when the application is serving, `False` otherwise.

```python
if not client.launch_app():
    raise SystemExit("could not start the application")
```

The transport is chosen here, not configured: the client asks the application whether it supports
WebSocket and uses REST when it does not.

---

### `connect(ip_address=None)`

Connects the tester and runs the version diagnostics. The exerciser licence is read from what comes
back, which is why it must happen before `run_exerciser()`.

| Parameter | Default | Meaning |
|---|---|---|
| `ip_address` | `None` | the tester. `None` uses the constructor's value, then the configured one |

**Returns** `{"success": True, "data": {...}}` once the application reports the tester connected,
or `{"success": False, "error": "..."}`:

```python
result = client.connect()
if not result["success"]:
    raise SystemExit("tester not reachable: {0}".format(result["error"]))

info = result["data"]
print(info.get("testerStatus"), info.get("firmwareVersion"))
```

The address can come from three places. The call wins, then the constructor, then the configuration
file.

---

### `run_compliance(project_name, esdf, test_cases)`

The whole compliance run: create the project, load the description file, sync the power profile to
it, select the cases, submit them, run them, and collect the report. `set_project` is the same call
under its earlier name, and takes the same arguments.

| Parameter | Default | Meaning |
|---|---|---|
| `project_name` | `None` | `None` uses `ProjectConfigurationModel` |
| `esdf` | `None` | description file relative to the application's folder, e.g. `"esdf/MyDevice.json"`. `None` uses `files.EsdfConfigurationModel` |
| `test_cases` | `None` | case names, or `["ALL"]` for every case the description file allows. `None` uses `Manual_test_cases.json`. An empty selection runs nothing |

**Returns** a one-element list describing the outcome:

| Return | Means |
|---|---|
| `["Test Execution completed"]` | every selected case reached a verdict |
| `["Test Execution INCOMPLETE: N of M selected case(s) never reached a verdict"]` | the application stopped early; the cases that never finished are named in the log |
| `["Test Execution did not start: no project was created. ..."]` | refused before anything ran — the description file could not be verified, its power profile could not be read, or the licence does not cover the profile. The reason is logged |
| `["Test Execution did not start: none of the N selected test case(s) is allowed by ..."]` | no selected name is a case the description file allows |
| `["No test cases selected for ..."]` | the selection was empty. Nothing was sent to the application |
| `["The test case selection for ... cannot be used ..."]` | the selection could not be read. Nothing was sent |
| `["esdf cannot be used for ..."]` | `is_multiple_esdf_files` is on, so a file or list passed for one run has nowhere to apply |
| `["Test Execution failed: ..."]` | the application rejected the submitted list |

**A refusal is never reported as a completion.** Only `["Test Execution completed"]` means the run
finished; `c3-run` exits `0` for that and `1` for anything else.

```python
print(client.run_compliance(
    project_name="MyProject",
    esdf="esdf/MyDevice.json",
    test_cases=[
        "8.1.1 PTX.CPX.PNG.S01.EPT.001",
        "8.1.2 PTX.CPX.PNG.S01.EPT.002",
    ],
))
```

**Case names must match what the description file makes applicable.** A name that does not is
dropped and named in the log rather than sent and silently ignored, so a typo costs one case and
not the run. An empty or unreadable selection is refused before anything is sent to the
application or the controller.

A description file supplied here is applied for this run only and put back afterwards; your
configuration file is not rewritten.

With `is_multiple_esdf_files` set, the run repeats once per description file in the `esdf` folder,
each with `Manual_test_cases.json`. Passing `esdf` or `test_cases` then is refused rather than
ignored.

---

### `run_exerciser(sequence_file, dry_run, hold)`

Drives the emulator from the sequence exported by the application's own UI, instead of running
compliance cases.

| Parameter | Default | Meaning |
|---|---|---|
| `sequence_file` | `None` | the sequence to use, relative to the application's folder, e.g. `"exerciser/ExerciserSequence-MPP15.json"`. `None` uses `files.ExerciserSequenceModel` |
| `dry_run` | `None` | compose every request and send none. `None` uses `common.exerciser.dry_run` |
| `hold` | `None` | seconds the exerciser runs before the session stops by itself. `None` runs until Enter is pressed, which needs a console |

**A sequence is required, and `c3-init` seeds one per power profile** into the application's
`exerciser\` folder, so a session runs without exporting anything first. Pass one by name, or leave
`sequence_file` as `None` to use the one named in the configuration. A bare file name is looked for
in `exerciser\` too.

To use a setup of your own, build it in the application's UI, export it, put the file in
`JSON_User_input\<application>\exerciser\` and pass or configure its name. Nothing is hand-typed
either way — what runs is what you built and saw in the application.

**Returns** a dict:

| Key | Meaning |
|---|---|
| `success` | whether the session completed |
| `error` | why it did not, when it did not |
| `dry_run` | true when nothing was sent |
| `results` | one record per step, plus `hold` and `stop` |
| `problems` | configuration problems found before anything was sent |
| `capture` | `copied_to` is the folder the evidence was copied to |
| `interrupted` | true when Ctrl+C ended it |

Each step record carries `op`, `success`, `verified` and `verify_detail`. `verified` is the part
that matters: the application returns success for a setting whether or not it took effect, so the
client reads the value back where it can.

| `verified` | Means |
|---|---|
| `changed` | the application's state moved after the write |
| `unchanged` | the application reports the same state before and after |
| `unavailable` | the application offers no read-back for this setting |
| `mismatch` | the read-back does not match what was sent |

A step the controller has no endpoint for is marked `skipped` — the four applications do not all
support the same settings, and that is not a failure.

```python
outcome = client.run_exerciser(sequence_file="exerciser/ExerciserSequence-MPP15.json", hold=60)
print("capture:", (outcome.get("capture") or {}).get("copied_to"))
```

**A session has no end of its own.** With `hold` it stops after that many seconds, and that counts
as success; pressing Enter in a console ends it sooner. Without `hold` it runs until you press
Enter. Ctrl+C also stops it cleanly, and every way the emulation is stopped and the capture
collected. A session that would wait for Enter is refused before anything is sent when no console
is attached — a scheduled task, or input redirected from `NUL` — so pass `hold`, or use `dry_run`.

The `hold` record in `results` says how it ended: `reason` is, for example, `hold limit reached`,
`operator` (Enter), `interrupted` (Ctrl+C) or `link lost`, and `held_seconds` is how long it ran.

---

### `disconnect()`

Stops live data, closes the HTTP session, and shuts the application down. Safe to call more than
once. **Always call it in a `finally`** — or use `with GRLApiClient(...)`, which does — or the
application is left running and the next run finds the port occupied.

---

### `get_telemetry()`

The live measurements captured during a run: power, signals and packets.

**Returns** a dict, or `{}` on REST builds, which do not stream live measurements.

---

### `call_api(api_name, ...)`

Calls one application endpoint directly, for anything the methods above do not cover.

| Parameter | Meaning |
|---|---|
| `api_name` | a member of `ApiName` |
| `data` | request body |
| `params` | query parameters |
| `endpoint_params` | values substituted into the route |
| `endpoint_override` | replace the route entirely |

```python
from grlps_c3_client import GRLApiClient
from API import ApiName

client = GRLApiClient()
client.launch_app()
print(client.call_api(ApiName.GET_SOFTWARE_VERSION))
```

`client.get_available_apis()` lists the names. `check_api_health()` and `is_api_ready()` report
whether the application is answering.

---

## Reference

### The shipped scripts

The commands are not a separate implementation. They call two modules that ship inside the package,
so you can read or import exactly what they run.

| Module | Backs | Does |
|---|---|---|
| `sample_run` | `c3-run`, `c3-exerciser` | one whole session, with the step-by-step reporting |
| `get_testcases` | `c3-testcases` | the applicable case list; runs nothing |

Both take the same inputs as the methods above and return a dict:

```python
from grlps_c3_client import sample_run, get_testcases

cases = get_testcases.main(app="GRL-C3-MP-TPR")["cases"]

outcome = sample_run.main(
    app="GRL-C3-MP-TPR",
    ip_address="192.0.2.77",
    mode="compliance",
    project="MyProject",
    esdf="esdf/MyDevice.json",
    test_cases=cases[:2],
)
```

Called with no arguments, `sample_run.main()` is exactly the configuration-based run. It also takes
`sequence_file`, `dry_run` and `hold` for an exerciser session. Each module also has a `cli()`
returning 0 or 1, which is what the commands use.

Before it starts anything, `sample_run.main()` prints what the run will use — the summary `c3-run`
and `c3-exerciser` show — and stops there if something would stop the run: no tester address, a
missing description file or sequence, nothing selected, or a session that would wait for Enter with
no console attached. Every reason is printed, so one attempt shows everything there is to fix.

It returns `launched`, `connected`, `mode`, and then `compliance` or `exerciser`. A step that never
happened leaves its key out, so the absence of a key tells you where it stopped. `refused` holds the
reasons when the run was stopped before anything started.

---

### What a run writes

| Folder | Holds |
|---|---|
| `logs\` | one log per run, and an HTTP trace when `trace_http` is on |
| `Run_time_files\<app>\` | what the application reported back during the run |
| `Runtime_Capture\<app>\` | one folder per run: the application's own report folder and `run_summary.json`, plus the exerciser capture |
| `Test_Case_List_From_System\<app>\` | the applicable list, and what was selected |

Nothing is written into the installed package.

When reporting a problem, include the log and the HTTP trace for the run that failed — together
they show every call the client made and what came back.

---

### Troubleshooting

**`ACTION REQUIRED: ... ip_address is still the placeholder`**, **`No tester address is set for ...`**
The tester address has not been set: run `c3-init --tester <address>`, adding `--app <name>` for an
application other than the selected one. `192.0.2.50` is a deliberately unroutable placeholder so an
unconfigured install fails fast. A script that passes `ip_address` does not need the configured
value set.

**`No test cases selected for ...`**
`Manual_test_cases.json` for that application is empty — which is how `c3-init` creates it — or a
script passed `test_cases=[]`. Name the cases, or use `["ALL"]` for every case the description file
allows. Nothing was started.

**`The test case selection for ... cannot be used`**
The message names the file and what is wrong with it: the line and column of a JSON error, an entry
that is not a name, or `"ALL"` given together with case names. Nothing was started.

**`This session runs until you press Enter, but no console is attached to read it`**
The session was started where nobody can press Enter — a scheduled task, or input redirected from
`NUL`. Pass `hold=SECONDS`, run it from a terminal, or set `common.exerciser.dry_run`.

**`RuntimeError` about Windows or the Python version at import**
The client requires Windows and CPython 3.11 or newer. Check with `python -V`.

**`No GRL configuration file at: ...`**
The path is relative and resolved against the directory you are in. Run from your workspace, pass
an absolute path, or set `GRL_C3_PROJECT_ROOT`.

**`No GRL C3 workspace in: ...`**
`c3-init` has not been run in this folder.

**`The exerciser sequence for ... is missing`**
The message lists the sequences that are there; point `files.ExerciserSequenceModel` at one of
them. `c3-init` seeds one per power profile, so this normally means the file was moved or renamed.
For a profile that is not seeded, export the sequence from the application's own UI into
`JSON_User_input\<application>\exerciser\`.

**The application does not start**
Check `app_path` for that application, and that the C3 software runs on its own. Close any copy
already running — a client that finds the port occupied talks to the copy it did not start and
cannot shut it down.

**`Controller is wrong. Expecting powerProfile in ...`**
The tester's firmware does not support the profile in the description file. The BPP/EPP firmware
exposes only its own profiles, whatever description file is selected.

**A case I asked for did not run**, **`Test Execution did not start: none of the ... selected test case(s) is allowed by ...`**
Names must match what the description file makes applicable. Unmatched names are named in the log.
Refresh the list with `c3-testcases` and copy them verbatim, or use `["ALL"]`.

**Zero applicable cases**
The description file does not describe the connected device, or the profile does not match. The
applicable count is per profile: `c3-testcases` prints it.

**A session that will not stop**
An exerciser session without `hold` runs until Enter. If no console is attached it is refused before
anything is sent, so nothing was started. From a script, pass `hold=SECONDS`.
