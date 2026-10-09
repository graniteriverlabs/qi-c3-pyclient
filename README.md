# GRL C3 Python Client

Python client for the **GRL Platform Solutions C3** wireless-charging test systems. It drives the C3
applications over their local API: start the application, connect to the tester, load a device
description file, run test cases or an exerciser session, and collect the report and capture
files — from the command line or from your own scripts.

```python
from grlps_c3_client import GRLApiClient

with GRLApiClient() as client:        # starts the application and connects the tester
    print(client.run_compliance())    # runs the cases selected in Manual_test_cases.json
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

```powershell
c3-init --tester 192.0.2.60
```

That writes the address for the selected application and changes nothing else. For another
application add `--app`:

```powershell
c3-init --app GRL-C3-TPT-MPP --tester 192.0.2.61
```

Or edit `grl_config.json` yourself — it is the same setting:

```json
"ip_address": "192.0.2.50"     →     "ip_address": "<your tester's address>"
```

`192.0.2.50` is the placeholder `c3-init` ships. It is unroutable on purpose, so an unconfigured
install fails immediately instead of reaching something real.

#### 3. Check what is configured

```powershell
c3-apps
```

```
Configuration  C:\benches\my-bench\grl_config.json

   APPLICATION          PORT   TESTER       STATUS
 * GRL-C3-MP-TPR        2002   192.0.2.60   select test cases
   GRL-C3-TPT-BPP-EPP   2004   not set      set the tester address
   GRL-C3-TPT-MPP       2004   not set      set the tester address
   GRL-WP-TPR-C3        3003   not set      application not installed

 * is used when --app is not given.

GRL-C3-MP-TPR will use
  Description file     esdf\MPP-25-esdf_20260901_153618.json
  Test cases           none selected - add case names, or "ALL", to
                       Test_Case_List_From_System\GRL-C3-MP-TPR\Manual_test_cases.json
  Exerciser sequence   exerciser\ExerciserSequence-MPP25.json
```

`*` marks the application the other commands use; change it with `Selected_app`. STATUS is the
first thing that would stop `c3-run`, checked without starting anything:

| STATUS | Means |
|---|---|
| `application not installed` | nothing at that application's `app_path` |
| `set the tester address` | `ip_address` is empty or still the placeholder |
| `description file not found` | the file `EsdfConfigurationModel` names is not there |
| `select test cases` | `Manual_test_cases.json` selects nothing |
| `fix the test case selection` | `Manual_test_cases.json` cannot be read |
| `ready` | `c3-run` can start |

`c3-apps --app <name>` shows what another application will use.

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

To run every case the description file allows, write:

```json
["ALL"]
```

| The file holds | `c3-run` |
|---|---|
| case names | runs the ones the description file allows; any it does not allow are named and skipped |
| `["ALL"]` | runs every case the description file allows |
| `[]`, nothing, or no file | **runs nothing**, and says which file to edit |
| invalid JSON, or not a list | **runs nothing**, and says what is wrong |
| `"ALL"` together with names | **runs nothing** — `"ALL"` must be the only entry |

Names must match the fetched list exactly — copy and paste them. `"ALL"` is matched in any case.

#### 7. Run

```powershell
c3-run
```

It prints what it will use, then starts:

```
Compliance run   GRL-C3-MP-TPR
  Tester             192.0.2.60
  Description file   esdf\MPP-25-esdf_20260901_153618.json
  Test cases         2 named in Manual_test_cases.json
```

Start the application → connect → create the project → load the description file → run the selected
cases → collect the report → shut down.

If something would stop the run — no tester address, a missing description file, nothing
selected — every reason is printed under that summary and **nothing is started**.

## Running tests

### Commands

Everything is set in the JSON files; the commands only run. The one exception is the tester
address, which `c3-init` can set for you.

| Command | Does |
|---|---|
| `c3-init` | create the workspace, or update an existing one after an upgrade |
| | seeds the configuration, example inputs, the exerciser sequences, and an empty test-case selection |
| `c3-apps` | each application and whether it is ready to run, then what the selected one will use |
| `c3-testcases` | fetch the applicable test cases; runs no tests |
| `c3-run` | run the selected test cases and collect the report |
| `c3-exerciser` | run an exerciser session |

`c3-run` and `c3-exerciser` print what they will use before they start, and start nothing when
something would stop them. Every command returns `0` on success and `1` on failure — for `c3-run`,
`0` means every selected case ran — so they drop straight into CI.

| Command | Option | Does |
|---|---|---|
| `c3-init` | `--tester ADDRESS` | write the tester address; nothing else in the configuration changes |
| | `--app NAME` | the application `--tester` is for, instead of `Selected_app` |
| | `--force` | reset to this version's defaults, after backing your configuration up |
| every other command | `--app NAME` | use this application for this run, instead of `Selected_app` |
| | `--config PATH` | use this configuration file, instead of the one in the workspace |

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

`c3-init --tester <address>` writes it for you. `c3-apps` prints the address each application will
use. See [Set your tester address](#2-set-your-tester-address).

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

A sequence is tied to **one power profile**, so `c3-init` seeds one per profile per application,
in that application's `exerciser\` folder:

| Application | Sequences |
|---|---|
| `GRL-C3-MP-TPR` | APP15, APP25, MPP15, MPP25 |
| `GRL-C3-TPT-MPP` | MPP15, MPP25 |
| `GRL-C3-TPT-BPP-EPP` | BPP, EPP |
| `GRL-WP-TPR-C3` | one sequence, which the export does not tie to a profile |

Choose one in `grl_config.json`, under that application's `files` block:

```json
"ExerciserSequenceModel": "exerciser/ExerciserSequence-MPP15.json"
```

**To use a sequence of your own**, export it from the application's UI into
`JSON_User_input\<application>\exerciser\` and name it there the same way.

You do **not** need to touch `run_mode`: `c3-run` always runs test cases and `c3-exerciser` always
runs a session, whatever the configuration was last left set to.

### Running it

```powershell
c3-exerciser
```

It prints what the sequence does before anything starts:

```
Exerciser session   GRL-C3-TPT-MPP
  Tester             192.0.2.61
  Sequence           exerciser\ExerciserSequence-MPP25.json
  Qi specification   2.3.1
  Power profile      MPP25
  Packets            26, in 2 sequences (11 and 15)
  Steps              13 - reset, 9 controller settings, phases, packets, start
  Not applied        gainConfig - this application cannot apply it
  Ends               when you press Enter
```

`Not applied` lists settings in the file the application has no endpoint for. A missing or unnamed
sequence file stops the command **before** the application starts, and lists the sequences that are
there.

A session runs until you press **Enter**; Ctrl+C also stops it cleanly. Either way the emulation is
stopped and the capture collected. Since it waits for a keypress, a session is **refused before
anything starts when no console is attached** — a scheduled task, or input redirected from `NUL`.
To run one unattended, use the Python API with a time limit:
[An exerciser session from a script](#an-exerciser-session-from-a-script).

To check your setup without sending anything to the tester, set this in `grl_config.json`:

```json
"common": { "exerciser": { "dry_run": true } }
```

That composes every request and sends none. Set it back to `false` for a real session.

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
| Project name | `run_compliance(project_name=...)` | `ProjectConfigurationModel` |
| Description file | `run_compliance(esdf=...)` | `files.EsdfConfigurationModel` |
| Test cases | `run_compliance(test_cases=[...])`, or `["ALL"]` | `Manual_test_cases.json` |
| Exerciser sequence | `run_exerciser(sequence_file=...)` | `files.ExerciserSequenceModel` |
| Dry run | `run_exerciser(dry_run=True)` | `common.exerciser.dry_run` |
| Session length | `run_exerciser(hold=60)` | until Enter is pressed |

Test cases follow the same rules as the file: `["ALL"]` runs every case the description file
allows, and an empty list runs nothing.

### A compliance run from a script

```python
from grlps_c3_client import GRLApiClient

with GRLApiClient(
    app="GRL-C3-MP-TPR",          # omit to use Selected_app
    ip_address="192.0.2.77",      # omit to use the configured address
) as client:
    print(client.run_compliance(
        project_name="MyProject",
        esdf="esdf/MyDevice.json",
        test_cases=["8.1.1 PTX.CPX.PNG.S01.EPT.001"],
    ))
```

Entering the `with` block starts the application and connects the tester; if either fails it
raises `RuntimeError` saying why, and the block does not run. Leaving it — normally or through an
exception — disconnects and closes the application.

The same steps one at a time, for a script that wants them separately:

```python
client = GRLApiClient(app="GRL-C3-MP-TPR")
try:
    if not client.launch_app():
        raise SystemExit("could not start the application")
    result = client.connect()
    if not result.get("success"):
        raise SystemExit("tester not reachable: {0}".format(result["error"]))
    print(client.run_compliance(test_cases=["ALL"]))
finally:
    client.disconnect()               # also closes the application
```

`run_compliance` does the whole run: create the project, load the description file, sync the power
profile, select the cases, submit, run, collect the report. `set_project` is the same call under its
earlier name. It returns one of:

| Return | Means |
|---|---|
| `["Test Execution completed"]` | every selected case reached a verdict |
| `["Test Execution INCOMPLETE: N of M ..."]` | stopped early; the unfinished cases are named in the log |
| `["Test Execution did not start: ..."]` | refused before anything ran — description file, profile, licence, or no allowed case selected |
| `["No test cases selected for ..."]` | the selection was empty; nothing was sent to the application |
| `["The test case selection for ... cannot be used ..."]` | the selection could not be read; nothing was sent |
| `["esdf cannot be used for ..."]` | `is_multiple_esdf_files` is on, so one file or list passed for a run has nowhere to apply |
| `["Test Execution failed: ..."]` | the application rejected the submitted list |

**Check each step's result rather than assuming it worked.** Only
`["Test Execution completed"]` means the run finished — a refusal is never a completion.

**Names must match what the description file makes applicable.** One that does not is dropped and
named in the log, so a typo costs one case, not the run. Get exact names with:

```python
from grlps_c3_client import get_testcases

print(get_testcases.main(app="GRL-C3-MP-TPR")["cases"])
```

### An exerciser session from a script

Same opening and closing; the one call differs. A script has nobody to press Enter, so give the
session a length:

```python
with GRLApiClient(app="GRL-C3-MP-TPR") as client:
    outcome = client.run_exerciser(
        sequence_file="exerciser/ExerciserSequence-MPP15.json",   # omit to use the configured one
        hold=60,                                                  # seconds, then it stops
    )
print("ok:", outcome["success"])
print("capture:", (outcome.get("capture") or {}).get("copied_to"))
```

The session stops itself after `hold` seconds, and that counts as success; the exerciser is always
stopped and the capture collected. Pressing Enter in a console ends it sooner. Without `hold`, a
script with no console is refused before anything starts.

### A whole run in one call

`sample_run` takes the same inputs and is what the commands themselves use, so a script gets the
same summary and the same refusals `c3-run` prints. With no arguments it is the
configuration-based run.

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

A workspace laid out by an earlier version is brought up to date too: exerciser sequences that sit
loose beside the configuration files are moved into their application's `exerciser\` folder, and a
setting that names a moved file is updated, after backing the configuration up. Each move is
reported; nothing is deleted.

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
│       ├── exerciser\                   one exerciser sequence per power profile
│       ├── project_config.json
│       ├── tester_config.json
│       ├── report_config.json
│       └── OptimumCoilValue*            optimum coil values
├── Test_Case_List_From_System\
│   └── <application>\
│       ├── Received_test_cases.json     generated: every applicable case
│       ├── Manual_test_cases.json       yours: which of them to run, or ["ALL"] (created empty)
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

**`ACTION REQUIRED: ... ip_address is still the placeholder`**, **`No tester address is set for ...`**
Step 2 has not been done: run `c3-init --tester <address>`, adding `--app <name>` for an
application other than the selected one. A script that passes `ip_address` does not need the
configured value.

**`No test cases selected for ...`**
`Manual_test_cases.json` for that application is empty — which is how `c3-init` creates it. Put
case names in it, or `["ALL"]` for every case the description file allows. Nothing was started.

**`The test case selection for ... cannot be used`**
The message names the file and what is wrong with it, for example the line of a JSON error, or
`"ALL"` given together with case names. Nothing was started.

**`This session runs until you press Enter, but no console is attached to read it`**
`c3-exerciser` was run where nobody can press Enter — a scheduled task, or input redirected from
`NUL`. Run it from a terminal, or from Python with `run_exerciser(hold=SECONDS)`.

**`Test Execution did not start: none of the ... selected test case(s) is allowed by ...`**
The names are not cases the description file allows. Refresh the list with `c3-testcases` and copy
the names verbatim, or use `["ALL"]`.

**`RuntimeError` about Windows or the Python version at import**
The client requires Windows and CPython 3.11 or newer. Check with `python -V`.

**`no matching distribution found`**
Your Python is older than 3.11. The package declares `requires-python = ">=3.11"`.

**`No GRL C3 workspace in: ...`**
`c3-init` has not been run here. Run it, or point `GRL_C3_PROJECT_ROOT` at an initialised folder.

**`No configuration file at: ...`**
The default path resolves against the directory you are in. Run from your workspace, or pass an
absolute path.

**`The exerciser sequence for ... is missing`**
The message lists the sequences that are there; point `files.ExerciserSequenceModel` at one of them.
One per profile is seeded, so this usually means the file was moved — `c3-init` puts it back. For a
profile that is not seeded, export it from the application's UI into
`JSON_User_input\<application>\`.

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
