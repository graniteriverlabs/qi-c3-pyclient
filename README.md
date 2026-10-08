# GRL C3 Python Client

Python client for the **GRL Platform Solutions C3** wireless-charging test systems. It drives the C3
applications over their local API: start the application, connect to the tester, load a device
description file, run test cases or an exerciser session, and collect the report and capture
files — from the command line or from your own scripts.

```python
from grlps_c3_client import GRLApiClient

client = GRLApiClient()
client.launch_app()
client.connect()
client.set_project()
client.disconnect()
```

## Contents

| Part | What is in it |
|---|---|
| **[Getting started](#getting-started)** | [Requirements](#requirements) · [Install](#install) · [Quickstart](#quickstart) — a bench from nothing to a finished run |
| **[Running tests](#running-tests)** | [Commands](#commands) · [Where your results are](#where-your-results-are) · [more than one application](#working-with-more-than-one-application) |
| **[Exerciser sessions](#exerciser-sessions)** | driving the emulator directly instead of running test cases |
| **[The Python API](#the-python-api)** | the same steps as library calls, for your own scripts |
| **[Keeping it current](#keeping-it-current)** | [Upgrading](#upgrading) · [The workspace](#the-workspace) |
| **[Reference](#reference)** | [Troubleshooting](#troubleshooting) · [Documentation](#documentation) · [License](#license) |

## Getting started

### Requirements

| | |
|---|---|
| OS | Windows |
| Python | 3.11 or newer (checked at install and at import) |
| Software | the GRL C3 application you want to drive, installed and licensed |
| Hardware | a reachable GRL C3 tester |

### Install

You do **not** need to clone this repository.

```powershell
pip install https://github.com/graniteriverlabs/qi-c3-pyclient/releases/download/v1.0.0/qi_c3_pyclient-1.0.0-py3-none-any.whl
```

That is the only install command. It covers both REST and WebSocket builds of the applications.

> **Names:** the distribution is `qi-c3-pyclient`; the module you import is `grlps_c3_client`.

### Quickstart

#### 1. Create a workspace

Make a folder for this bench and initialise it there:

```powershell
mkdir C:\benches\my-bench
cd C:\benches\my-bench
c3-init
```

`c3-init` seeds the configuration, the example inputs and the exerciser sequences into **the
directory you are standing in**, then prints what still needs setting. Run the other commands from
this same folder. It never overwrites your edits, and is safe to run again — do run it after
upgrading, which is how new settings reach an existing workspace. See
[Upgrading](#upgrading).

#### 2. Set your tester address

Open `grl_config.json` and replace the placeholder for the application you use:

```json
"ip_address": "192.0.2.50"     →     "ip_address": "<your tester's address>"
```

`192.0.2.50` is unroutable on purpose, so an unconfigured install fails immediately instead of
reaching something real. Check `app_path` points at your installation while you are there.

#### 3. Check what is configured

```powershell
c3-apps
```

```
APPLICATION             PORT  TESTER           INPUTS     SEQUENCE   INSTALLED
------------------------------------------------------------------------------
* GRL-C3-MP-TPR         2002  192.0.2.50       9 file(s)  yes        yes
  GRL-C3-TPT-BPP-EPP    2004  192.0.2.50       7 file(s)  yes        yes
  GRL-C3-TPT-MPP        2004  192.0.2.50       7 file(s)  yes        yes
  GRL-WP-TPR-C3         3003  192.0.2.50       6 file(s)  yes        yes
```

`*` marks the application the other commands use; change it with `Selected_app`. `INSTALLED` is
whether `app_path` exists, `SEQUENCE` whether the configured exerciser file is present. It also
lists the exerciser sequences each application has.

#### 4. Add your device description file

Copy your ESDF into that application's folder and point the configuration at it:

```powershell
copy C:\path\to\MyDevice.json JSON_User_input\GRL-C3-MP-TPR\esdf\
```

```json
"EsdfConfigurationModel": "esdf/MyDevice.json"
```

An example description file ships for each application, so the flow runs before you supply your
own. Both the WPC signed schema 3.0 format and the older unsigned schema 2.0 are accepted.

#### 5. See which tests your description file allows

```powershell
c3-testcases
```

The applicable cases depend on the description file, so this loads it first. **No tests are run.**
The full list is written to:

```
Test_Case_List_From_System\<application>\Received_test_cases.json
```

Add `--out mylist.json` to write it somewhere else as well.

#### 6. Choose the tests to run

`c3-init` creates `Manual_test_cases.json` empty in the same folder. Put the names you want
in it:

```json
[
  "8.1.1 PTX.CPX.PNG.S01.EPT.001",
  "8.1.2 PTX.CPX.PNG.S01.EPT.002"
]
```

Names must match the fetched list exactly — copy and paste them. Anything that does not match is
skipped and named in the log. **While the file is empty, every applicable case is selected** — an
empty selection means no choice has been made, not a choice of nothing.

#### 7. Run

```powershell
c3-run
```

Start the application → connect → create the project → load the description file → run the selected
cases → collect the report → shut down.

## Running tests

### Commands

| Command | Does |
|---|---|
| `c3-init` | create the workspace, or update an existing one after an upgrade |
| | seeds the configuration, example inputs, the exerciser sequences, and an empty test-case selection |
| `c3-apps` | list the configured applications and their settings |
| `c3-testcases` | fetch the applicable test cases; runs no tests |
| `c3-run` | run the selected test cases and collect the report |
| `c3-exerciser` | run an exerciser session |

Every command returns `0` on success and `1` on failure, so they drop straight into CI.

Every command except `c3-init` also takes these two options:

| Option | Does |
|---|---|
| `--app NAME` | use this application for this run, instead of `Selected_app` |
| `--config PATH` | use this configuration file, instead of the one in the workspace |

### Where your results are

Everything a run produces goes under your workspace — the folder you ran `c3-init` in. Nothing
is written into the installed package.

| What | Where |
|---|---|
| **The application's own reports** | `Runtime_Capture\<application>\run_<timestamp>\application_report\` |
| What this client recorded about the run | `Runtime_Capture\<application>\run_<timestamp>\run_summary.json` |
| Exerciser session evidence | `Runtime_Capture\<application>\exerciser_<timestamp>\` |
| The run's log | `logs\grl_api_run_<timestamp>.log` |
| Every request and reply, for support | `logs\grl_api_trace_<timestamp>.jsonl` |
| Cases your description file allows | `Test_Case_List_From_System\<application>\Received_test_cases.json` |
| What the application reported back | `Run_time_files\<application>\` |

`application_report\` is the application's own report folder, copied as it wrote it:

```
Runtime_Capture\GRL-C3-MP-TPR\run_20261008-160941\application_report\
└── <report folder, named by the application>\
    ├── *_GRL_C3_FinalReport.pdf / .html / .json   the final report, three formats
    ├── *eSDF_Report.json                          the device description report
    ├── *_Final_TestBackup.gproj                   project backup
    └── Run1\
        ├── *_GRL_C3_FinalReport.pdf               this run's report
        ├── *_GRL_C3_Report_*.html / .json         this run's results
        ├── <test case>\<test case>.html           one page per test case
        ├── <test case>\<test case>.grltrace       protocol trace for that case
        └── ReferenceData\DebugLogger_*.log        the application's debug log
```

The run prints the folder it wrote to when it finishes.

**The tester address is a setting, not a result.** It goes in `grl_config.json`, under the
application you are driving:

```json
"applications": {
  "GRL-C3-MP-TPR": {
    "ip_address": "<your tester's address>"
  }
}
```

`c3-apps` prints the address each application will use. See
[Set your tester address](#2-set-your-tester-address).

### Working with more than one application

One install drives all four. One workspace holds all four side by side, each with its own input
folder; a second workspace is only for a second set of settings.

| Application | Emulates | Typical port |
|---|---|:--:|
| `GRL-C3-MP-TPR` | a receiver, to test a transmitter | 2002 |
| `GRL-C3-TPT-MPP` | a transmitter, to test a receiver | 2004 |
| `GRL-WP-TPR-C3` | a receiver, to test a transmitter | 3003 |
| `GRL-C3-TPT-BPP-EPP` | a transmitter, in BPP/EPP firmware | 2004 |

REST and WebSocket builds are both detected automatically — nothing to configure.

Without `--app`, every command uses the one marked `*` by `c3-apps`:

```powershell
c3-testcases --app GRL-C3-TPT-MPP
c3-run       --app GRL-C3-TPT-MPP
c3-exerciser --app GRL-C3-TPT-MPP
```

To use a workspace from a different folder, point at it:

```powershell
set GRL_C3_PROJECT_ROOT=C:\benches\my-bench
```

## Exerciser sessions

The exerciser drives the emulator directly instead of running compliance cases.

### What it needs

**One sequence file.** It holds the packet sequence, controller settings and phase timings, and
comes from the application's own UI export — nothing is typed into this client.

A sequence is tied to **one power profile**, so `c3-init` seeds one per profile per application:

| Application | Sequences |
|---|---|
| `GRL-C3-MP-TPR` | APP15, APP25, MPP15, MPP25 |
| `GRL-C3-TPT-MPP` | MPP15, MPP25 |
| `GRL-C3-TPT-BPP-EPP` | BPP, EPP |
| `GRL-WP-TPR-C3` | one sequence, which the export does not tie to a profile |

Pick one for a session by the profile it is for:

```powershell
c3-exerciser --sequence MPP15
```

`c3-apps` lists them and marks the one used when `--sequence` is not given:

```
Exerciser sequences available, per application (c3-exerciser --sequence <profile>):
  GRL-C3-MP-TPR          APP15, APP25, MPP15, MPP25 (in use)
  GRL-C3-TPT-BPP-EPP     BPP (in use), EPP
```

To make a profile the default, name its file in `grl_config.json` under that application's
`files` block:

```json
"ExerciserSequenceModel": "ExerciserSequence-MPP15.json"
```

**To use a sequence of your own**, export it from the application's UI into
`JSON_User_input\<application>\`, then name it in the configuration or pass it to `--sequence`.
Anything named `ExerciserSequence*.json` is listed by `c3-apps` with the shipped ones.

You do **not** need to touch `run_mode`: `c3-run` always runs test cases and `c3-exerciser` always
runs a session, whatever the configuration was last left set to.

### Running it

```powershell
c3-exerciser
```

A missing or unnamed sequence file stops the command **before** the application starts, naming the
path it looked for.

A session runs until you press **Enter**; Ctrl+C also stops it cleanly. Either way the emulation is
stopped and the capture collected. Since it waits for a keypress, a session is **refused before
anything is sent when no console is attached** — run it from a terminal, not a scheduled task.

To check your setup without touching the hardware:

```powershell
c3-exerciser --dry-run
```

That composes every request and sends none.

### What you will see

Each setting is reported with what the read-back confirmed — the application returns success
whether or not a setting took effect:

```
Exerciser session: OK
  ok   coil_type        accepted; the application offers no read-back
  ok   packets          accepted; application state unchanged
  ok   start            the application reported itself busy
  ok   hold             ran 01:16, 16 readings, packets 0 -> 25 (operator)
  capture: Runtime_Capture\GRL-C3-MP-TPR\exerciser_20260929-110812
```

A step the controller has no endpoint for is reported as not available, not as a failure: the four
applications do not all support the same settings.

## The Python API

Every step the commands take is a library call. Compliance runs and exerciser sessions are both
driven this way.

Two ways to work, and they mix freely:

- **Configuration-based** — everything in `grl_config.json`, steps called with no arguments. This
  is what the commands do.
- **Script-based** — pass what you want for that run. **Anything you leave out falls back to the
  configuration file**, and nothing is written back, so a script never disturbs configured values.

| Input | In the script | Falls back to |
|---|---|---|
| Application | `GRLApiClient(app=...)` | `Selected_app` |
| Tester address | `GRLApiClient(ip_address=...)` | that application's `ip_address` |
| Project name | `set_project(project_name=...)` | `ProjectConfigurationModel` |
| Description file | `set_project(esdf=...)` | `files.EsdfConfigurationModel` |
| Test cases | `set_project(test_cases=[...])` | `Manual_test_cases.json`, or every applicable case |
| Exerciser sequence | `run_exerciser(sequence_file=...)` | `files.ExerciserSequenceModel` |

### A compliance run from a script

```python
from grlps_c3_client import GRLApiClient

client = GRLApiClient(
    app="GRL-C3-MP-TPR",          # omit to use Selected_app
    ip_address="192.0.2.77",      # omit to use the configured address
)
try:
    if not client.launch_app():
        raise SystemExit("could not start the application")

    result = client.connect()
    if "error" in result:
        raise SystemExit("tester not reachable: {0}".format(result["error"]))

    print(client.set_project(
        project_name="MyProject",
        esdf="esdf/MyDevice.json",
        test_cases=["8.1.1 PTX.CPX.PNG.S01.EPT.001"],
    ))
finally:
    client.disconnect()               # also closes the application
```

`set_project` does the whole run: create the project, load the description file, sync the power
profile, select the cases, submit, run, collect the report. It returns one of:

| Return | Means |
|---|---|
| `["Test Execution completed"]` | every selected case reached a verdict |
| `["Test Execution INCOMPLETE: N of M ..."]` | stopped early; the unfinished cases are named in the log |
| `["Test Execution did not start: ..."]` | refused before anything ran — description file, profile, or licence |
| `["Test Execution failed: ..."]` | the application rejected the submitted list |

**Check each step's result rather than assuming it worked**, and check for `"completed"` rather
than for a non-empty list — a refusal is never a completion.

**Names must match what the description file makes applicable.** One that does not is dropped and
named in the log, so a typo costs one case, not the run. Get exact names with:

```python
from grlps_c3_client import get_testcases

print(get_testcases.main(app="GRL-C3-MP-TPR")["cases"])
```

`test_cases=[]` selects nothing and runs nothing. Asking for no cases is not asking for all.

### An exerciser session from a script

Same opening and closing; the one call differs:

```python
outcome = client.run_exerciser(
    sequence_file="ExerciserSequence-MPP15.json",   # omit to use the configured one
    dry_run=False,
)
print("capture:", (outcome.get("capture") or {}).get("copied_to"))
```

### A whole run in one call

`sample_run` takes the same inputs and is what the commands themselves use, so a script gets the
same reporting `c3-run` prints. With no arguments it is the configuration-based run.

```python
from grlps_c3_client import sample_run

outcome = sample_run.main(
    app="GRL-C3-MP-TPR",
    mode="compliance",                                  # or "exerciser"
    esdf="esdf/MyDevice.json",
    test_cases=["8.1.1 PTX.CPX.PNG.S01.EPT.001"],
)
```

[`examples/`](examples/) has complete scripts — one supplying everything from the script, one
relying entirely on the configuration file.

## Keeping it current

### Upgrading

```powershell
pip install --upgrade https://github.com/graniteriverlabs/qi-c3-pyclient/releases/download/v1.0.0/qi_c3_pyclient-1.0.0-py3-none-any.whl
cd C:\benches\my-bench
c3-init
```

**Run `c3-init` again after upgrading.** A new version can add settings, and your
`grl_config.json` already exists, so only `c3-init` puts them there. It reports what it added:

```
Added 2 setting(s) new in this version:
  common.exerciser.new_control
  applications.GRL-C3-BRAND-NEW
```

**Nothing you have set is changed** — only missing keys are added, and missing files are put
back. With nothing to do it prints nothing.

To start over with this version's defaults instead:

```powershell
c3-init --force
```

That replaces the configuration and the shipped example inputs, **after copying your
configuration to `grl_config.json.bak-<timestamp>`**. Your own files — description files, exported
sequences, your test-case selection — are never touched. Use it to reset, not to upgrade.

### The workspace

Everything lives in the directory where you ran `c3-init`:

```
your-bench-folder\
├── grl_config.json                      applications, addresses, file choices, run settings
├── JSON_User_input\
│   └── <application>\                   one folder per application
│       ├── esdf\                        put your description files here
│       ├── project_config.json
│       ├── tester_config.json
│       ├── report_config.json
│       ├── OptimumCoilValue*            optimum coil values
│       └── ExerciserSequence*.json      one exerciser sequence per power profile
├── Test_Case_List_From_System\
│   └── <application>\
│       ├── Received_test_cases.json     generated: every applicable case
│       ├── Manual_test_cases.json       yours: which of them to run (created empty)
│       └── selected_test_cases.json     generated: what was actually selected
├── Run_time_files\<application>\        generated: what the application reported back
├── Runtime_Capture\<application>\       generated: the application's own reports, per run
└── logs\                                generated: one log and one HTTP trace per run
```

Nothing is written into the installed package.

To run the commands from somewhere else, point them at the folder:

```powershell
set GRL_C3_PROJECT_ROOT=C:\benches\my-bench
```

| Variable | Effect |
|---|---|
| `GRL_C3_PROJECT_ROOT` | use this folder as the workspace, wherever you run from |
| `GRL_C3_HOME` | where `c3-init` creates the workspace, instead of the current directory |

Importing the library never creates files — only `c3-init` writes anything.

## Reference

### Troubleshooting

**`ACTION REQUIRED: ... ip_address is still the placeholder`**
Step 2 has not been done. A script that passes `ip_address` does not need the configured value.

**`RuntimeError` about Windows or the Python version at import**
The client requires Windows and CPython 3.11 or newer. Check with `python -V`.

**`no matching distribution found`**
Your Python is older than 3.11. The package declares `requires-python = ">=3.11"`.

**`No GRL C3 workspace in: ...`**
`c3-init` has not been run here. Run it, or point `GRL_C3_PROJECT_ROOT` at an initialised folder.

**`No configuration file at: ...`**
The default path resolves against the directory you are in. Run from your workspace, or pass an
absolute path.

**`The exerciser sequence configured for ... is missing`**
`c3-apps` lists what each application has; pass one to `--sequence` or name it under
`files.ExerciserSequenceModel`. One per profile is seeded, so this usually means the file was moved
— `c3-init` puts it back. For a profile that is not seeded, export it from the application's UI
into `JSON_User_input\<application>\`.

**The application does not start**
Check `app_path` for that application, and that the C3 software runs on its own. Close any copy
already running — a client that finds the port occupied talks to the copy it did not start and
cannot shut it down.

**`Controller is wrong. Expecting powerProfile in ...`**
The tester's firmware does not support the profile in the description file. The BPP/EPP firmware
exposes only its own profiles, whatever description file is selected.

**A case I asked for did not run, or the run found zero cases**
Case names must match what the description file makes applicable, and the applicable set differs
per power profile. Unmatched names are named in the log. Refresh the list with `c3-testcases` and
copy the names verbatim.

Logs and HTTP traces for every run are written to `logs\`. Include both when reporting a problem —
together they show every call the client made and what came back.

### Documentation

| Guide | Covers |
|---|---|
| [User guide](grlps_c3_client/docs/USER_GUIDE.md) | every method, its inputs and what it returns, the workspace, and troubleshooting |

The guide ships inside the package. Find it with:

```python
from grlps_c3_client import docs_dir
print(docs_dir())
```

### License

Use of this software is governed by the GRL Platform Solutions Software End User License
Agreement — see [LICENSE](LICENSE).

This is **proprietary, source-available** software, not open source. The source is published so you
can read it, script against it and debug integrations. A valid licence from GRL Platform Solutions is
required to use it, and the EULA does not permit redistribution, sublicensing, or reverse
engineering.

GRL, GRL Platform Solutions and the GRL logo are trademarks of GRL Platform Solutions.
