# Changelog

Todos los cambios relevantes se registran aquí. / All notable changes are recorded here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). The release notes of each version are taken from this file.

## [Unreleased]

### Changed

- New logo: an open book with the aucove graph on its pages. The central vertex, in amber, is the shared repository and sits on the spine.
- The `requirements-lock.txt` of the next release pins `mcp` and `mcp-types` 2.3.0 (were 2.2.0) and `tzdata` 2026.5 (was 2026.4).
- The package summary on PyPI is in English, and the PyPI page links to the repository, the user guide, the issues and the changelog.

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
