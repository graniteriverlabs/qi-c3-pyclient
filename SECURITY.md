# Security

## Reporting a vulnerability

Please report security issues privately to Granite River Labs rather than opening a public
issue. We will acknowledge your report and tell you what we intend to do about it.

## What this client handles

The client talks to a GRL C3 application over HTTP on the local machine, and to a tester on
your own network. It does not send anything to Granite River Labs.

Two things worth knowing when you share files from a run:

- **Logs and HTTP traces** under `logs/` record every call made, including your tester's
  address and the contents of your device description file.
- **Capture folders** under `Runtime_Capture/` contain the report archives produced by the
  application, which describe the device under test.

Review both before attaching them to a ticket that leaves your organisation.
