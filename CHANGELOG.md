# Changelog

Todos los cambios relevantes se registran aquí. / All notable changes are recorded here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). The release notes of each version are taken from this file.

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
