# Changelog

Todos los cambios relevantes se registran aquí. / All notable changes are recorded here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). The release notes of each version are taken from this file.

## [Unreleased]

### Added

- `init --repo <owner>/<repo>` creates the project in an empty folder, for an empty GitHub repository, without cloning the framework. It writes the published template of the installed version with the answers to the usual questions, creates the git repository with `origin`, and pushes it in the `init` commit after the confirmation. Before writing anything it checks that the folder is empty, that the repository has no branches and that git has the identity for the commit; if writing, committing or pushing fails, it removes what it created and leaves the folder empty again. In a folder that is already the project with that `origin`, it works like `init`, so running it again completes a run that failed after the push. With `--dry-run`, it lists the plan and writes nothing.
- The README explains two ways to start a project, from the terminal or with the agent in a single folder; the lead guide uses `init --repo`, and the framework's `AGENTS.md` tells agents what to do when the person wants to use aucove rather than develop it.

### Changed

- The complete render of the template, with `CODEOWNERS`, `task.yml` and `.mcp.json`, lives in the core, and `tools/render_template.py` uses it, so the template repository and `init --repo` give the same files. The wheel carries `tools/template_vars.toml`.
- The messages of `E_STATE_DIRTY_WORKTREE`, `E_CONFIG_REMOTE_MISMATCH`, `E_CONFIG_GIT_UNAVAILABLE` and `E_AUTH_NOT_AUTHENTICATED` cover the cases of `init --repo`, and the hint of `E_FORGE_PUSH_REJECTED` says what to do when git has no credentials to push (`permission`).

## [0.1.0rc2] - 2026-10-15

Second preliminary release of `0.1.0`. It adds the project signature, so that anyone can check that a record was issued by the project and not only when it existed, and it brings what publishing `0.1.0rc1` showed: a fix in the GitHub adapter, the PyPI page, the installation commands that `doctor` suggests, and a new logo.

### Added

- Project signature: each project can have its own signing key (`ssh-ed25519`), kept by the admin of the repository, so that anyone can check that a record comes from the project, not only when it existed. The public keys go in `evidence_signing` of `project.yaml`, each with the dates it is valid; an old key is never removed, so its signatures still verify.
- With `evidence_signing.mode: ci`, the `integration` job of `seal.yml` signs `manifest.json` with the key stored as the secret `AUCOVE_EVIDENCE_KEY` of the environment `aucove-evidence`, and the timestamps cover the signature (`manifest.json.sig`). Without the secret, or with a key that is not a current key of the project, the record is still sealed without the signature, and the job says why in an annotation, in its summary and in the pull request comment. Submissions are never signed.
- `evidence sign <package> --key <file>` (human only): with `mode: admin`, the admin signs the `index.txt` of a milestone package with `ssh-keygen` and timestamps the signature in a `signature/` folder.
- `evidence verify` gives the origin of each record: `project`, `unknown` or `unsigned`. It uses the list of keys that the group publishes (`--keys`) or, inside the project, the keys of the base `project.yaml`. On a package it also verifies each `signature*/` folder and the files that its index lists. `VERIFY.md` explains how to check the signature with `ssh-keygen` alone.
- `init` creates the environment `aucove-evidence` with the base branch as its only deployment rule, and refuses the key as a secret of the repository or of the organization. `doctor` checks the mode, that a key is valid now and the environment.

### Changed

- The `evidence` extra declares `cryptography>=44`, which signs and verifies the project signature (it already came with `rfc3161-client`).
- `evidence verify` always reports a `signature` check. Records sealed with `0.1.0rc1` stay valid and show as unsigned.
- The administration token that `init` asks for also needs read access to Secrets and Environments, to check where the project key is kept.
- New logo: an open book with the aucove graph on its pages. The central vertex, in amber, is the shared repository and sits on the spine.
- The `requirements-lock.txt` of this release pins `mcp` and `mcp-types` 2.3.0 (were 2.2.0), `tzdata` 2026.5 (was 2026.4) and `cryptography` 50.0.2 (was 50.0.1), whose wheels are built with OpenSSL 4.0.3.
- The package summary on PyPI is in English, and the PyPI page links to the repository, the user guide, the issues and the changelog.
- The workflows of the template and of the framework use `actions/upload-artifact` 7.0.2 (was 7.0.1) and `actions/download-artifact` 8.0.2 (was 8.0.1).

### Fixed

- GitHub adapter: when a branch or tag write is rejected or its response is lost, the confirmation read is repeated after 0.5 and 1 s if GitHub still shows the previous state of the ref, since GitHub can serve it for a moment after a write. Before, claiming a task that someone had claimed a moment earlier could fail with a validation error instead of reporting that the task is already claimed, deleting a branch that was just deleted could fail instead of reporting that it did not exist, and an update whose response was lost could fail as a timeout although it was applied.
- The installation commands that `doctor` and the `E_CONFIG_CORE_MISMATCH` error suggest apply the `requirements-lock.txt` of the release, like the README, and `aucove --help` no longer says that the project must use LaTeX.
- The README no longer asks for `--force` to add the `evidence` extra to an existing installation: `uv tool install` adds it, or changes the version, without it.
- The PyPI page shows the logo and working links: the release rewrites the relative paths of the README, which is the package description, as absolute links pinned to the release tag. The README in the repository does not change.

## [0.1.0rc1] - 2026-10-12

First public version of the framework. `0.1.0rc1` is a preliminary release; the Claude Code hooks and skills, the published template repository and the devcontainer arrive with `0.1.0`.

### Added

- Core package with a CLI and an MCP server (stdio) generated from a single operation registry: `init`, `doctor`, `status`, `context`, `task propose`, `task accept`, `task cancel`, `claim`, `release`, `switch`, `save`, `update`, `submit`, `record-assessment`, `review list`, `review show`, `review approve`, `review request-changes`, `decision propose`, `direction propose`, `expire-claims`, `sync-roles`, `validate`, `check-path`, `metrics` and `evidence verify`.
- Human-only operations (`init`, accepting and cancelling tasks, approving and requesting changes) need a terminal and a typed confirmation; the MCP server does not expose them.
- Atomic task claims on a `task/<n>` branch, per-part permissions from `roles.yaml`, session notes, overlap assessments and the `enforced` and `advisory` forge profiles.
- Guided project setup: `init` creates and accepts the lead's setup task ("Prepare the project"), and `context` warns while the plan or the people are still the template's (`setup_pending`), so the agent offers to prepare the project in plain language. With `0.1.0`, the start skill runs that interview and, to anyone with no task in progress, shows their parts and the open tasks in them.
- GitHub adapter (REST) and a fake adapter with fault injection, with a contract suite that runs against both.
- Project template (`template/`) in Spanish and English, for LaTeX (`format: latex`) or without a compiled document (`format: none`), with CI workflows that validate, build the PDF and comment it on the pull request, report a broken base branch, expire stale claims and seal every submission and merge with verifiable timestamps (RFC 3161 and OpenTimestamps).
- Evidence verification for third parties (`evidence verify` and the `VERIFY.md` kit), with the optional `evidence` extra.
- User guide in English and Spanish (`docs/en/` and `docs/es/`): how it works and where the code runs, the researcher's and the lead's guides, publishing and safeguarding the evidence, what to do when something happens, and four example teams.
- Release pipeline: `release.yml` publishes to PyPI by Trusted Publishing, with PEP 740 and GitHub attestations, a CycloneDX SBOM and a `requirements-lock.txt` with hashes in each GitHub Release; `nightly.yml` runs the contract suite against real GitHub and checks that the published template matches its render.
- Licensing and governance of the framework repository: `LICENSE` (Apache-2.0), `NOTICE`, `LICENSES/`, `REUSE.toml` (REUSE 3.3), SPDX headers in every Python file, `CONTRIBUTING.md` (DCO and AI-assistance policy), `CODE_OF_CONDUCT.md`, `SECURITY.md`, `AGENTS.md`, `CLAUDE.md`, the pull request template, the maintenance guide (`.github/MAINTAINING.md`) and the v0.1 specification in `spec/` (CC-BY-4.0).
