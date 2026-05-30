# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.0.x   | ✅ Yes    |
| < 1.0   | ❌ No     |

## Threat Model

ASTra MCP is a **local-only tool**. It:
- Never sends code or embeddings to any external server
- Never makes outbound network requests (except model download on first install)
- Stores all data in `.astra/` inside your project directory
- Runs over Unix domain socket (localhost only)

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Email: [nsatyasaicharan@gmail.com](mailto:nsatyasaicharan@gmail.com)

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Your suggested fix (optional)

You'll get a response within 48 hours. If confirmed, a patch will be released within 7 days and you'll be credited in the release notes.

## Known Safe Behaviors

| Behavior | Status |
|---|---|
| Code never leaves your machine | ✅ By design |
| No telemetry or analytics | ✅ Verified |
| No API keys required | ✅ Embeddings run locally |
| SQLite DB readable only by current user | ✅ OS file permissions |
| Dashboard binds to 127.0.0.1 only | ✅ Not exposed on network |
| Unix socket at `~/.astra/daemon.sock` | ✅ Localhost only |

## Scope

In scope: code execution, data exfiltration, privilege escalation, path traversal in indexer.

Out of scope: social engineering, physical access, issues in upstream dependencies (report those to the dependency maintainer).
