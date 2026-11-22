# Changelog

## 1.1.0

- **API mode on MPP-TPR and MPP-TPT WebSocket builds.** The client takes control of the
  application over its WebSocket and answers every pop-up there. The window keeps showing the run,
  with **API mode** and a red **!** icon while the client is in control. This needs an application
  build with API mode. On an older WebSocket build, the client stops with that reason before it
  touches the tester. REST builds work as before.
- **Taking control back at the bench.** Selecting **UI mode** in the window stops the running
  test. The run ends with `Test Execution STOPPED: control was taken back at the bench`, and the
  client leaves the application open for the person using it.
- **New settings `open_and_close_app` and `app_host`**, under `common` or per application:
  - `true` (the default) opens the application on this PC if it is not running and closes it
    after the run, as before;
  - `false` uses the application that is already open, on this PC or on the PC named by
    `app_host`, and leaves it open.

  Every problem with these settings is reported with the setting, its value and what to change.
- **macOS.** The client runs on macOS against the application on a Windows PC
  (`open_and_close_app` false, `app_host` set to that PC).
- **Reports and exerciser captures are downloaded through the application**, from the folder the
  application names and once it has finished writing it. A run from macOS saves the same files as
  a run on the bench PC.
- **The client no longer clicks the application's window**, and `pywinauto` is no longer
  installed.
- `c3-init` checks `app_path` only for an application the client opens itself.

## 1.0.0

First release.

- Drives all four GRLPS C3 applications from one install: MPP-TPR, TPT in its MPP firmware,
  C3-TPR, and TPT in its BPP/EPP firmware. The application is chosen per run with `--app`
  or by `Selected_app`, without editing anything else.
- REST and WebSocket builds are detected automatically.
- Compliance runs: create the project, load a WPC signed or legacy description file, sync the
  power profile, fetch and filter the applicable cases, run them, answer operator pop-ups, and
  collect the report archive and per-case captures.
- Exerciser sessions driven by the sequence file exported from the application's own UI,
  with per-step read-back and a guaranteed stop.
- Five commands: `c3-init`, `c3-apps`, `c3-testcases`, `c3-run`, `c3-exerciser`.
- Workspace model: configuration, inputs and all output live in the directory you run in.
- Every input can come from the configuration file or from your script, with the script
  winning and the configuration as the fallback: application, tester address, project name,
  description file, test cases and exerciser sequence.
- User guide shipped inside the package, covering every method, its inputs and what it
  returns, the workspace layout and troubleshooting.
