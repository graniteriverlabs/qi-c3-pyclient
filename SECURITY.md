# Security

## Reporting a vulnerability

Please report security issues privately to GRL Platform Solutions rather than opening a public
issue. We will acknowledge your report and tell you what we intend to do about it.

## What this client handles

The client talks to a GRLPS C3 application over HTTP and WebSocket, and the application connects
to the tester on your own network. The client does not send anything to GRL Platform Solutions.

| Version | Where the application is |
|---|---|
| 1.0.0 | on the same PC as the client |
| 1.1.0 (beta) | on the same PC by default, or on the Windows PC named by `app_host` |

When `app_host` is another PC, that traffic crosses your network. It is not encrypted, and the
application does not ask for a sign-in, so anyone who can reach its port can drive it. Keep the
bench PC, and the computer that runs the client, on a network you trust.

Two things worth knowing when you share files from a run:

- **Logs and HTTP traces** under `logs/` record every call made, including your tester's
  address, the bench PC's address and the contents of your device description file.
- **Capture folders** under `Runtime_Capture/` contain the reports produced by the
  application, which describe the device under test.

Review both before attaching them to a ticket that leaves your organisation.
