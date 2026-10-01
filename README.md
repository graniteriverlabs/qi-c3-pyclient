# GRL C3 Python Client

Python client for the **Granite River Labs C3** wireless-charging test systems. It drives the C3
applications over their local API: start the application, connect to the tester, load a device
description file, run test cases or an exerciser session, and collect the report and capture
files — from the command line or from your own scripts.

```python
from grl_c3_client import GRLApiClient

client = GRLApiClient()
client.launch_app()
client.connect()
client.set_project()
client.disconnect()
```

## Requirements

| | |
|---|---|
| OS | Windows |
| Python | 3.11 or newer (checked at install and at import) |
| Software | the GRL C3 application you want to drive, installed and licensed |
| Hardware | a reachable GRL C3 tester |

## Install

You do **not** need to clone this repository.

```powershell
pip install https://github.com/graniteriverlabs/qi-c3-pyclient/releases/download/v1.0.0/qi_c3_pyclient-1.0.0-py3-none-any.whl
```

That is the only install command. It covers both REST and WebSocket builds of the applications.

> **Names:** the distribution is `qi-c3-pyclient`; the module you import is `grl_c3_client`.

## Quickstart

### 1. Create a workspace

Make a folder for this bench and initialise it there:

```powershell
mkdir C:\benches\my-bench
cd C:\benches\my-bench
c3-init
```

`c3-init` copies a configuration file and a set of example inputs into **the directory you are
standing in**, then prints what still needs setting. Run the other commands from this same folder.

Your edits are never overwritten. Run `c3-init` again whenever you like — and do run it after
upgrading the package, because that is how settings a new version adds reach a workspace that
already exists. See [Upgrading](#upgrading).

### 2. Set your tester address

Open `grl_config.json` and replace the placeholder for the application you use:

```json
"ip_address": "192.0.2.50"     →     "ip_address": "<your tester's address>"
```

`192.0.2.50` is a deliberately unroutable documentation address, so an unconfigured install fails
immediately instead of reaching something real. Check `app_path` points at your installation while
you are there.

### 3. Check what is configured

```powershell
c3-apps
```

```
APPLICATION           PORT  TESTER           INPUTS    INSTALLED
* GRL-C3-MP-TPR       2002  192.0.2.77         6 file(s) yes
  GRL-C3-TPT-MPP      2004  192.0.2.50       6 file(s) yes
  GRL-WP-TPR-C3       3003  192.0.2.50       6 file(s) yes
  GRL-C3-TPT-BPP-EPP  2004  192.0.2.50       6 file(s) yes
```

`*` marks the application the other commands will use. To change it, set `Selected_app` in
`grl_config.json`.

### 4. Add your device description file

Copy your ESDF into that application's folder and point the configuration at it:

```powershell
copy C:\path\to\MyDevice.json JSON_User_input\GRL-C3-MP-TPR\esdf\
```

```json
"EsdfConfigurationModel": "esdf/MyDevice.json"
```

An example description file ships for each application, so the flow runs before you supply your
own. Both the WPC signed schema 3.0 format and the older unsigned schema 2.0 are accepted.

### 5. See which tests your description file allows

```powershell
c3-testcases
```

The applicable cases depend entirely on the description file, so this loads it first. **No tests
are run.** The full list is written to:

```
Test_Case_List_From_System\<application>\Received_test_cases.json
```

Add `--out mylist.json` to write it somewhere else as well.

### 6. Choose the tests to run

Put the names you want in `Manual_test_cases.json`, in the same folder:

```json
[
  "8.1.1 PTX.CPX.PNG.S01.EPT.001",
  "8.1.2 PTX.CPX.PNG.S01.EPT.002"
]
```

Names must match the fetched list exactly — copy and paste them. Anything that does not match is
skipped and named in the log. If the file is absent, every applicable case is selected.

### 7. Run

```powershell
c3-run
```

Start the application → connect → create the project → load the description file → run the selected
cases → collect the report → shut down.

## Commands

| Command | Does |
|---|---|
| `c3-init` | create the workspace, or update an existing one after an upgrade |
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

## Working with more than one application

One install drives all four, and one workspace holds all four side by side, each with its own input
folder. A second workspace is only needed when you want a second set of settings.

| Application | Emulates | Typical port |
|---|---|:--:|
| `GRL-C3-MP-TPR` | a receiver, to test a transmitter | 2002 |
| `GRL-C3-TPT-MPP` | a transmitter, to test a receiver | 2004 |
| `GRL-WP-TPR-C3` | a receiver, to test a transmitter | 3003 |
| `GRL-C3-TPT-BPP-EPP` | a transmitter, in BPP/EPP firmware | 2004 |

REST and WebSocket builds are both supported and detected automatically — there is nothing to
configure.

Without `--app`, every command uses the application marked `*` by `c3-apps`. With it, they use the
one you name:

```powershell
c3-testcases --app GRL-C3-TPT-MPP
c3-run       --app GRL-C3-TPT-MPP
c3-exerciser --app GRL-C3-TPT-MPP
```

To use a workspace from a different folder, point at it:

```powershell
set GRL_C3_PROJECT_ROOT=C:\benches\my-bench
```

## Exerciser mode

The exerciser drives the emulator directly instead of running compliance cases.

### What it needs

**One file, and no configuration changes.**

The packet sequence, controller settings and phase timings are not typed into this client. You set
them up **in the application's own UI**, export them, and drop the exported file into that
application's input folder — so what runs is what you built and saw in the application.

1. In the application, set up the exerciser and **export** the configuration. It writes a file
   named `ExerciserSequenceConfigurationData-*.json`.
2. Put that file in the application's input folder:

   ```
   JSON_User_input\GRL-C3-MP-TPR\
   ```

3. Name it in `grl_config.json`, under that application's `files` block:

   ```json
   "ExerciserSequenceModel": "ExerciserSequenceConfigurationData-tpr.json"
   ```

One is already configured for each application as an example, so `c3-exerciser` runs before you
export your own. `c3-apps` shows whether each application has its file:

```
APPLICATION             PORT  TESTER       INPUTS     SEQUENCE   INSTALLED
* GRL-C3-MP-TPR         2002  192.0.2.77     6 file(s)  yes        yes
```

Nothing else changes. In particular you do **not** need to touch `run_mode` — each command says
which mode it is, so `c3-run` always runs test cases and `c3-exerciser` always runs a session,
whatever the configuration file was last left set to.

### Running it

```powershell
c3-exerciser
```

If the sequence file is missing or unnamed, the command says so and stops **before** starting the
application, naming the exact path it looked for.

A session has no end of its own: it runs until you press **Enter**. Ctrl+C also stops it cleanly,
and either way the emulation is stopped and the capture collected.

Because it waits for a keypress, a session that starts the emulator is **refused before anything is
sent when no console is attached** — so run it from a terminal, not from a scheduled task.

To check your setup without touching the hardware:

```powershell
c3-exerciser --dry-run
```

That composes every request and sends none.

### What you will see

Each setting is reported with what the read-back confirmed, because the application returns success
for a setting whether or not it took effect:

```
Exerciser session: OK
  ok   coil_type        accepted; the application offers no read-back
  ok   packets          accepted; application state unchanged
  ok   start            the application reported itself busy
  ok   hold             ran 01:16, 16 readings, packets 0 -> 25 (operator)
  capture: Runtime_Capture\GRL-C3-MP-TPR\exerciser_20260929-110812
```

A step the controller has no endpoint for is reported as not available rather than as a failure —
the four applications do not all support the same settings.

## Using it from Python

There are two ways to work, and they mix freely.

**Configuration-based** — put everything in `grl_config.json` and call the steps with no
arguments. This is what the commands do.

**Script-based** — pass what you want in the script. Anything you pass is used for that run;
**anything you leave out falls back to the configuration file.** Nothing is written back, so a
script that supplies its own values never disturbs the configured ones.

That applies to every input:

| Input | In the script | Falls back to |
|---|---|---|
| Application | `GRLApiClient(app=...)` | `Selected_app` |
| Tester address | `GRLApiClient(ip_address=...)` | that application's `ip_address` |
| Project name | `set_project(project_name=...)` | `ProjectConfigurationModel` |
| Description file | `set_project(esdf=...)` | `files.EsdfConfigurationModel` |
| Test cases | `set_project(test_cases=[...])` | `Manual_test_cases.json`, or every applicable case |
| Exerciser sequence | `run_exerciser(sequence_file=...)` | `files.ExerciserSequenceModel` |

### Running test cases from a script

```python
from grl_c3_client import GRLApiClient

client = GRLApiClient(
    app="GRL-C3-MP-TPR",          # omit to use Selected_app
    ip_address="192.0.2.77",        # omit to use the configured address
)
try:
    if not client.launch_app():
        raise SystemExit("could not start the application")

    result = client.connect()
    if "error" in result:
        raise SystemExit("tester not reachable: {0}".format(result["error"]))

    # Everything here is optional. Leave an argument out and the configured value is used.
    print(client.set_project(
        project_name="MyProject",
        esdf="esdf/MyDevice.json",
        test_cases=[
            "8.1.1 PTX.CPX.PNG.S01.EPT.001",
            "8.1.2 PTX.CPX.PNG.S01.EPT.002",
        ],
    ))
finally:
    client.disconnect()
```

`set_project` does the whole compliance run: create the project, load the description file, sync
the power profile, select the cases, submit them, run, and collect the report.

**The names must match what the description file makes applicable.** A name that does not is
dropped and named in the log rather than sent and silently ignored, so a typo costs you one case,
not the run. Get the exact names with `c3-testcases`, or in a script:

```python
from grl_c3_client import get_testcases

outcome = get_testcases.main(app="GRL-C3-MP-TPR")
print(outcome["cases"])          # every applicable case, exactly as named
```

Passing `test_cases=[]` selects nothing and runs nothing — asking for no cases is not the same as
asking for all of them.

### Running an exerciser session from a script

```python
from grl_c3_client import GRLApiClient

client = GRLApiClient(app="GRL-C3-MP-TPR")
try:
    client.launch_app()
    client.connect()

    outcome = client.run_exerciser(
        sequence_file="ExerciserSequenceConfigurationData-tpr.json",   # omit to use the config
        dry_run=False,
    )
    print("capture:", (outcome.get("capture") or {}).get("copied_to"))
finally:
    client.disconnect()
```

### Or drive the whole run in one call

`sample_run` takes the same inputs and is the module the commands themselves use, so a script gets
the same reporting `c3-run` prints:

```python
from grl_c3_client import sample_run

outcome = sample_run.main(
    app="GRL-C3-MP-TPR",
    ip_address="192.0.2.77",
    mode="compliance",
    project="MyProject",
    esdf="esdf/MyDevice.json",
    test_cases=["8.1.1 PTX.CPX.PNG.S01.EPT.001"],
)
```

Called with no arguments it is exactly the configuration-based run:

```python
sample_run.main()
```

### Checking each step

Check the result of each step rather than assuming it worked — a failed description-file load
otherwise lets the run continue against whatever the application had loaded before, and every case
comes back with nothing to say why.

See [`examples/`](examples/) for complete scripts, including one that supplies everything from the
script and one that relies entirely on the configuration file.

## Upgrading

```powershell
pip install --upgrade https://github.com/graniteriverlabs/qi-c3-pyclient/releases/download/v1.0.0/qi_c3_pyclient-1.0.0-py3-none-any.whl
cd C:\benches\my-bench
c3-init
```

**Run `c3-init` again after upgrading.** A new version can add settings — a new run control, a new
application — and your `grl_config.json` already exists, so nothing would put them there. `c3-init`
merges them in and tells you what it added:

```
Added 2 setting(s) new in this version:
  common.exerciser.new_control
  applications.GRL-C3-BRAND-NEW
```

**Nothing you have set is changed.** Your tester addresses, your chosen application, your file
choices and your run settings are left exactly as they are; only keys that are missing get added.
Files that have gone missing are put back at the same time. Running it when there is nothing to do
prints nothing and changes nothing.

To start over with this version's defaults instead:

```powershell
c3-init --force
```

That replaces the configuration and the shipped example inputs, **after copying your configuration
to `grl_config.json.bak-<timestamp>`**. Files you added yourself — your own description files, your
own exported sequences — are never touched. Use it to reset, not to upgrade.

## The workspace

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
│       └── ExerciserSequenceConfigurationData-*.json   exported from the application's UI
├── Test_Case_List_From_System\
│   └── <application>\
│       ├── Received_test_cases.json     generated: every applicable case
│       ├── Manual_test_cases.json       yours: which of them to run
│       └── selected_test_cases.json     generated: what was actually selected
├── Run_time_files\<application>\        generated: what the application reported back
├── Runtime_Capture\<application>\       generated: report archives, packet logs, captures
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

## Troubleshooting

**`ACTION REQUIRED: ... ip_address is still the placeholder`**
Step 2 has not been done. `192.0.2.50` is a deliberately unroutable placeholder so an unconfigured
install fails fast. A script that passes `ip_address` does not need the configured value set.

**`RuntimeError` about Windows or the Python version at import**
The client requires Windows and CPython 3.11 or newer. Check with `python -V`.

**`no matching distribution found`**
Your Python is older than 3.11. The package declares `requires-python = ">=3.11"`.

**`No GRL C3 workspace in: ...`**
`c3-init` has not been run in this folder. Run it, or point `GRL_C3_PROJECT_ROOT` at a folder you
have already initialised.

**`No GRL configuration file at: ...`**
The default path is relative and resolves against the directory you are in. Run from your
workspace, or pass an absolute path.

**`The exerciser sequence file for ... is missing`**
Export the sequence from the application's own UI, put it in `JSON_User_input\<application>\`, and
name it under `files.ExerciserSequenceModel`. `c3-apps` shows which applications have theirs.

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

## Documentation

| Guide | Covers |
|---|---|
| [User guide](grl_c3_client/docs/USER_GUIDE.md) | every method, its inputs and what it returns, the workspace, and troubleshooting |

The guide ships inside the package. Find it with:

```python
from grl_c3_client import docs_dir
print(docs_dir())
```

## License

Use of this software is governed by the GRL Platform Solutions Software End User License
Agreement — see [LICENSE](LICENSE).

This is **proprietary, source-available** software, not open source. The source is published so you
can read it, script against it and debug integrations. A valid licence from Granite River Labs is
required to use it, and the EULA does not permit redistribution, sublicensing, or reverse
engineering.

GRL, Granite River Labs and the Granite River Labs logo are trademarks of Granite River Labs.
