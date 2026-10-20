# {{var:title}}

Collaborative research project, written by a team that works with AI assistants on git.
Each person works in their part, `sections/<section-id>/`: text, code, figures, tables and notes.
The `aucove` core (pinned version: `{{var:core_pin}}`) does the mechanical work: tasks, branches, commits, PRs and validation.
It works with Claude Code, Codex and other AI tools that read `AGENTS.md`.

## What is in this repository

| Path | Contents |
|---|---|
| `plan.md` | Project direction: questions, hypotheses, scope and methods, with stable IDs. |
| `roles.yaml` | People, roles, deputy and the owner of each part. |
| `memory/constitution.md` | Principles, style and rules of the project. |
| `memory/decisions/` | Decision log: files are only added, never modified. |
| `AGENTS.md`, `CLAUDE.md` | Rules for AI agents. |
| `sections/<section-id>/` | One folder per part, with a single owner. |
| `tex/main.tex` | Main manuscript (with `format: latex`); it only includes the other files. |
| `tex/outline/` | One file per article section, with its title and its parts in order (with `format: latex`). |
| `tex/guide.md` | Article guide: what each section and each part covers, the notation and the tone (with `format: latex`). |
| `bib/references.bib` | Export of the project's Zotero group (with `format: latex`). |
| `data/README.md` | Index of the datasets, which are not versioned. |
| `project.yaml` | Configuration: convention, language, format, forge and build. |

## Set up your computer

Do this once on each computer, with [aucove's initial setup](https://github.com/{{var:framework_repo}}/blob/v{{var:core_pin}}/README.md#initial-setup): your GitHub account with the private email, git with your identity, the core, your GitHub token for this repository in `GH_TOKEN`, the clone of this repository and your agent open in that folder.
If you would rather have your agent with you, use [Setting up aucove with your agent](https://github.com/{{var:framework_repo}}/blob/v{{var:core_pin}}/docs/en/setup-with-your-agent.md).

This project uses the `aucove` core `{{var:core_pin}}`.
Install and update it only from a terminal opened from the Start menu (or Terminal on macOS), never from inside your agent's app: on Windows, whatever is installed from inside Claude Desktop stays in a separate copy that only that app sees.
First you need [uv](https://docs.astral.sh/uv/).
On macOS or Linux:

```sh
LOCK="$(mktemp)"
curl -fsSL -o "$LOCK" https://github.com/{{var:framework_repo}}/releases/download/v{{var:core_pin}}/requirements-lock.txt
UV_COMPILE_BYTECODE=1 uv tool install "aucove=={{var:core_pin}}" --constraints "$LOCK"
uv tool update-shell
```

On Windows (PowerShell):

```powershell
$lock = New-TemporaryFile
Invoke-WebRequest -UseBasicParsing https://github.com/{{var:framework_repo}}/releases/download/v{{var:core_pin}}/requirements-lock.txt -OutFile $lock.FullName
$env:UV_COMPILE_BYTECODE = "1"; uv tool install "aucove=={{var:core_pin}}" --constraints $lock.FullName
uv tool update-shell
```

Close the terminal, open a new one and check everything with `aucove doctor` in the project folder.

## If you are the lead

1. Fill in `roles.yaml` with the people and the owner of each part, and `plan.md` with the project direction.
2. Adjust `memory/constitution.md` and section 7 of `AGENTS.md` with the project's own rules.
3. Run `aucove sync-roles` to regenerate `.github/CODEOWNERS` and the task form.
4. Run `aucove validate` before submitting any change.
5. You or your deputy review and approve PRs; an agent never approves or merges.
6. With `format: latex`, you are also the article editor, unless you declare another one in `roles.yaml`: you order the sections in `tex/outline/`, write the article guide in `tex/guide.md` and request changes from the owners of the parts.

## If you are a researcher

1. Ask the lead to add you to `roles.yaml` and to assign you a part.
2. Always work inside a task: propose it or claim it before editing.
3. Write only in your folder `sections/<section-id>/`, with one sentence per line. If `project.yaml` says `format: latex`, paths in `\input` and `\includegraphics` start with `../sections/<section-id>/`.
4. Before editing a file, check whether you may do so with `aucove check-path <path>`.
5. Cite only keys from the Zotero group; if one is missing, paste its export into your part's `pending.bib` (only with LaTeX).

## Build locally

With `format: latex`, from the root of the repository, `latexmk` uses `.latexmkrc`, which is equivalent to the CI build.
The PDF ends up in `tex/build/`, which git ignores.
On Windows with MiKTeX, `latexmk` needs Perl, which MiKTeX does not include (TeX Live does): first install [Strawberry Perl](https://strawberryperl.com/).
For example, with `winget install --id StrawberryPerl.StrawberryPerl -e` in a terminal opened from the Start menu, and then open a new terminal.

## Licenses

The code of this template (configuration, workflows and the lead's `.tex` files) is under MIT-0, and the example text is under CC0-1.0.
The per-path detail is in `REUSE.toml`.
When the project is initialized, the lead chooses the licenses of its text, code and data.

## Non-affiliation notice

*aucove* is an independent open source project. It is not affiliated with, endorsed by, or sponsored by Anthropic, OpenAI, Google, GitHub, or any other provider mentioned. Claude, Claude Code, Codex, Gemini, GitHub, and other marks belong to their respective owners and are mentioned only to indicate compatibility.
