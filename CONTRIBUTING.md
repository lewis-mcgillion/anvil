# Contributing to Anvil

Thank you for helping improve Anvil. Please read this guide before opening an
issue or pull request.

## Before you start

* Search existing issues and pull requests before proposing a duplicate.
* For security vulnerabilities, do **not** open a public issue. Follow
  [SECURITY.md](SECURITY.md).
* Changes must preserve the contracts and replay safety rules in
  [docs/architecture.md](docs/architecture.md).

## Development workflow

1. Fork the repository and create a focused branch from the default branch.
2. Make the smallest change that addresses the problem.
3. Add or update tests for success, rejection, edge, and regression cases
   whenever executable code is changed.
4. Run the relevant checks locally and include the commands and results in the
   pull request.
5. Open a pull request using the repository template.

Pull requests should explain the problem, the chosen approach, compatibility
impact, and any operational or security considerations. Keep unrelated
refactors out of the change.

## Design and compatibility

Public interfaces require an explicit compatibility assessment. Document
schema or API changes and update the corresponding compatibility documentation.
Errors should be actionable and deterministic; cancellation must stop work
promptly; resources must be released on success, failure, and cancellation.
Replay must never execute production tools.

## Commit and review expectations

Use clear, imperative commit messages. Reviewers may request changes when a
pull request lacks tests, weakens a contract, exposes sensitive information,
or does not document a deliberate compatibility change. Maintainers merge
only after required checks pass and review concerns are resolved.

By submitting a contribution, you agree that it is provided under the
repository's [Apache License 2.0](LICENSE) and that you have the right to
submit it.
