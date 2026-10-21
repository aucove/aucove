# Changelog

Todos los cambios relevantes se registran aquí. / All notable changes are recorded here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). The release notes of each version are taken from this file.

## [0.1.0rc5] - 2026-10-20

Fifth preliminary release of `0.1.0`. It brings what the first test session of a project of a single person taught: `aucove/base-audit` no longer fails after every merge through a PR, the agent gets the text of the declaration that it asks the person to confirm and a field for the definition of done of the tasks it proposes, and the guides cover working alone and the first steps after the setup task.

### Added

- `task propose` and `propose_task` take the definition of done (`--done`, `done`), one condition per item and from 1 to 20; before, that section always said `_No response_`. A summary that carries a heading of the form, such as `### Definition of done`, is rejected with `E_VALIDATION_INPUT` (`reason: form_heading`): it repeated a field, and aucove could no longer read the task.
- "If something fails" covers three new cases: `main` turning red because `aucove/base-audit` ends with `permission`, with the change by hand for projects created with an earlier version; a submission that cannot build the PDF on the computer because MiKTeX has no Perl; and a task that `status` shows with `parts: -`.
- The lead guide has a box for projects of a single person (`mode: individual`). It also explains that the submission is a PR with the same title as the task, how to accept several tasks at once and how to claim them.

### Changed

- The README's initial setup installs Git Credential Manager on macOS and Linux too, as Git for Windows already does: git signs in to GitHub in the browser with the permission to push the project's workflows, which `init --repo` needs, and aucove uses that session for every push. Before `init`, the lead signs in once with `git ls-remote`, because aucove runs git without prompts.
- Each project's README says that on Windows with MiKTeX, `latexmk` needs Perl, which MiKTeX does not include, and how to install Strawberry Perl.
- The hints of `E_AUTH_NOT_AUTHENTICATED` and `E_FORGE_PUSH_REJECTED` point to Git Credential Manager instead of `gh auth setup-git`, which hands git the `GH_TOKEN` token, without the Workflows permission, so the first push of `init` failed with it.
- The description of the `submit_task` tool carries the whole text of the `attest-v1` declaration, which the agent shows the person before calling it with `sign_off: true` (11-trazabilidad REQ-TRACE-23); the researcher guide shows it too.
- The setup task that `init` creates lists the topics of the interview and asks for one question at a time. The README, the lead guide and the agent guide give the exact message for the agent and say that the lead edits no file.
- When the token does not see the repository, `doctor` names the three common causes: its owner is not the organization, the repository is not selected, or the approval is pending. When the token sees every repository of its owner, it says that editing the token is enough. The README says that `GH_TOKEN` holds one token at a time.
- The comments of the template's `roles.yaml` list the valid values of `account_type` and the format of `credit`, and say that a consumer account needs `require_commercial: false` and a decision. The Perl note of each project's README also says to restart the agent's app.
- With `compile_on_submit: if_available`, `submit` checks with `latexmk -v` that latexmk runs before building: one that exists but does not run, such as MiKTeX's without Perl, counts as missing, and `local-build` is skipped, since CI builds the PDF anyway.
- `propose_task`, `propose_decision` and `propose_direction` also look for a plan ID that the local copy of the base lacks in the base on the forge: after a change to the plan is merged, `origin/<base>` stays behind until the next fetch.
- The slug of a new decision is cut at the last hyphen that fits, without splitting a word. Existing IDs do not change.

### Fixed

- `aucove/base-audit` failed after every merge through a PR, in every project: to audit a merged PR it reads the required checks of its head, and the `audit` job of `main-broken.yml` lacked `checks: read` and `statuses: read` (09-ci §7.1). Projects created with an earlier version add both to the `permissions` line of that job by hand, in a task of the lead, as "If something fails" describes.
- When `ci audit` fails because of the forge, it prints the call that was denied and, with a 403, the permission that GitHub names, not only `permission`.
- `task accept` refuses a task whose body it cannot read (`E_CLAIM_NO_SECTIONS`, with `details.reason`), instead of accepting a task with no parts.
- With `mode: individual`, `submit` no longer warns `W_PERM_DEPUTY_MISSING`: the mode has no deputy and no bypass (00-marco D-57), as `doctor` and `init` already said.

## [0.1.0rc4] - 2026-10-16

Fourth preliminary release of `0.1.0`. It brings what the first real run of `init --repo` against GitHub taught: the README's initial setup, step by step and by role, with a guide to do it with an agent; `doctor` finds the second copy of aucove that Claude Desktop keeps on Windows; and every release now installs itself from PyPI on Linux, macOS and Windows.

### Added

- A guide to set up aucove with an agent (`docs/es/configurar-con-agente.md`, `docs/en/setup-with-your-agent.md`): the messages to paste at each stage, by role, and what the agent checks and never does, such as installing, running `init` or asking for the token.
- After each release, `release.yml` installs the published version from PyPI as the README says, with the Release's `requirements-lock.txt`, on Linux, macOS and Windows, and checks its version, the template that the wheel renders and that `init --repo --dry-run` refuses to run in CI.
- `doctor` warns when a packaged Windows app, such as Claude Desktop, keeps its own copy of aucove in its private storage, which that app uses instead of the one installed from a terminal, and says how to remove it.

### Changed

- The readable output of `init` counts the new files instead of listing every one, describes what it did on GitHub in words, and its texts no longer cite the specification. When the organization's Actions policy cannot be read, the pending steps say where to check it on the web.
- Each project's README links to the initial setup, at the tag of its `core_pin`, and its installation commands match the README's: install only from a terminal outside the agent's app, with `UV_COMPILE_BYTECODE`.
- In each project's `AGENTS.md`, item 5 of section 2 asks the agent to tell the person which setup step is missing when `doctor` reports a mismatch, a missing token or a missing git identity, without doing it itself and without ever asking for the token (02-convencion §8.3). Projects created with an earlier version get `conv.agents.modified` on section 2 when they move to this one, until the lead copies that item from the template.
- The live and e2e test runs in CI list failures and errors in their summary (`-rfEs`), not only skips.
- In the first two days of a project, `doctor` no longer warns that `expire-claims` has never run, since the workflow runs once a day.
- Outside a project, `doctor` points to `init --repo <owner>/<repo> --dry-run`, which checks the token, the empty repository and git without writing anything.
- The README's "Installation" and "Getting started" become a single "Initial setup" in eight steps that say who does each one, the lead or a researcher: the GitHub account and the `noreply` email, the organization and the empty repo, git and its identity, aucove, the everyday token, storing it in `GH_TOKEN` on each system without showing it and checking it, the project (`init --repo`, with the one-day administration token and its own prefilled link, or `git clone`) and the agent. The researcher and lead guides link to it instead of repeating it, and "If something fails" covers a token that the terminal sees but the agent does not.
- The README and the guides: install and update aucove only from a terminal outside the agent's app, check `aucove --version` from the agent, quit Claude Desktop from its tray icon, check `init` first with `--dry-run`, and a new case in "If something fails" for a second copy of aucove.

### Fixed

- The researcher guide sets git's name as well as the `noreply` email, after installing git; the lead guide asks for it before `init --repo`, which stops without writing anything when git has no identity, and "If something fails" covers that case.

## [0.1.0rc3] - 2026-10-15

Third preliminary release of `0.1.0`. With `init --repo`, a project starts from an empty folder and an empty GitHub repository, without cloning the framework: preparing a project with the agent on `0.1.0rc2` took two sessions, one of them in the framework's own clone.

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
