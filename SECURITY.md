# Security Policy

## Supported version

Security fixes currently target the latest released minor version.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this repository. Do not open a public issue for a vulnerability that could expose secrets, execute commands without authorization, bypass workflow validation, corrupt state, or invalidate audit records.

Include the affected version, reproduction steps, impact, and any suggested mitigation. Do not include real credentials or private runtime state.

## Operational guidance

- Treat workflow JSON as executable configuration.
- Review command arguments and HTTP destinations before granting authorization.
- Keep each runtime home directory and its `audit.key` private.
- Do not publish SQLite state, adapter receipts, command output, or audit keys.
- Run untrusted workflows in an isolated operating-system account or container.
