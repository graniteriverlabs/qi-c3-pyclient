# Changelog

## 1.0.0

First release.

- Drives all four GRL C3 applications from one install: MPP-TPR, TPT in its MPP firmware,
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
