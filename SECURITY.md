# Security policy

## Reporting a vulnerability

Please do not report security vulnerabilities in a public issue or pull
request. Use GitHub's **Report a vulnerability** private advisory workflow for
this repository. If that workflow is unavailable, contact the maintainers
privately through the repository owner profile and include “Anvil security
report” in the subject.

Include:

* the affected version or commit;
* a concise description and impact;
* reproduction steps or a proof of concept;
* the conditions required to exploit it; and
* a suggested mitigation, if known.

Please redact credentials, personal data, and other secrets. We will
acknowledge a report when received, investigate it, and coordinate disclosure
of a fix and credit with the reporter. Do not publicly disclose the issue
until a fix or mitigation has been released.

## Scope

Reports are especially valuable for authentication, authorization, secret
handling, unsafe tool execution, replay isolation, data exposure, and
dependency or build-pipeline compromise. Denial of service against shared
infrastructure and vulnerabilities in unsupported third-party systems are
outside the project's direct scope, but may still be reported for awareness.

## Supported versions

There is no released versioned runtime yet. Until the first release, report
issues against the default branch and the latest published commit.
