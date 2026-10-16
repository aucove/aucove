# When something happens

[User guide](README.md) › When something happens

You do not need to learn this beforehand. If it happens, the agent or CI tells you and suggests what to do.

## With your work

<details>
<summary>You are asked for changes</summary>

You come back with `/start` and the task number, fix it and submit again. It is a new round of the same submission, and it is sealed again.

</details>

<details>
<summary>You want to switch tasks</summary>

Tell the agent "switch to 24": it saves what is pending and brings up the other one. You can have up to 3 active tasks; those in review do not count.

</details>

<details>
<summary>You are away for a few days</summary>

A task expires after 14 days without activity, with a warning 3 days before. Expiring deletes nothing: if you come back and save, it is still yours. If you do not come back, the lead can hand it to someone else, and your work is archived.

</details>

<details>
<summary>You need a new macro or reference</summary>

With LaTeX, the agent writes the explicit form and leaves the macro request for the paper's editor. References are added to Zotero and pasted into `pending.bib` in your part; the editor moves them into the shared bibliography.

</details>

<details>
<summary>You work from the browser</summary>

Everything works the same, except releasing a task, which is done from the terminal or from Claude Desktop. Open each session with a single repository: with several, the project rules are not loaded. Remember that on the web the code runs on a cloud machine that only sees the repo ([Where the code runs](how-it-works.md#where-the-code-runs)).

</details>

## With other people

<details>
<summary>Your work clashes with another task</summary>

When you claim and when you submit, the agent compares your work with the active tasks. If it sees a possible clash, such as the same macro defined in two ways, it leaves it as a warning on the submission and mentions the lead. It blocks nothing: the lead decides, usually with a decision.

</details>

<details>
<summary>Someone changed something you use</summary>

Ask the agent to "update" to bring the new changes into your task. If the change is in a file that is not yours and it breaks your work, the agent does not fix it on its own: it escalates to the lead, and the task is marked as blocked, without expiring, until it is resolved.

</details>

<details>
<summary>The shared version stops building</summary>

CI opens a notice with the suspect commits, and everyone sees it in `/project-status`. Whoever broke it submits a fix. When it builds again, the notice closes by itself.

</details>

<details>
<summary>The plan no longer fits</summary>

With `/direction` you propose the change and cite the affected question or hypothesis. The lead decides and edits the plan. The submissions that touch that part of the plan are flagged for a careful review.

</details>

## Data and people

<details>
<summary>Where does the project data go?</summary>

Raw data never enters the repository, whether it comes from simulations, measurements, surveys or clinical records. It stays where it already lives: your computer, the cluster, the hospital's server or a data repository. `data/README.md` says what it is, where it is and how to request access.

What reaches the repo is the analysis code, the aggregated results (tables, figures and metrics) and, in `provenance.yaml`, which script produced each result and the SHA-256 fingerprint of the data it used. No file in the repo can exceed 5 MiB; models and large datasets go to their own repository, such as Hugging Face, Zenodo or your institution's.

</details>

<details>
<summary>Can the agent see my data?</summary>

Using real data is normal. What changes with how sensitive it is, is whether the AI may see it. The lead sets this when preparing the project:

- **Public or internal**, unpublished but with no personal data, which is the most common case: yes. The agent can read and analyze it on your computer, with a commercial account or, if the group agreed to it, with a consumer account with training turned off.
- **Restricted**: personal data even if pseudonymized, health data, data under a confidentiality agreement or with ethics committee conditions. In v0.1 it is not given to the AI, except under an institutional zero data retention contract with the committee's approval, and always outside the repository. The usual approach is for the agent to help write the code with test data of the same shape (synthetic data) and for the person to run it on the real data, without AI, wherever it lives.
- **Local only**, data that cannot leave the institution: only with models that run on the institution's own infrastructure.

Remember that what the agent reads, including the output of the commands it runs, reaches the model ([Where the code runs](how-it-works.md#where-the-code-runs)).

</details>

<details>
<summary>Someone does not use AI</summary>

They can work with aucove without any agent. They do the same as everyone else in "Before the first day" (install aucove, create their token and clone the repo), except for the agent, and write their parts with the editor they prefer, such as TeXstudio or VS Code, inside the project folder. Their day looks like this:

```bash
aucove                                    # the board: tasks and parts
aucove claim 30                           # claims task 30 and creates its branch
# … edits their files with their editor …
aucove save -m "Clinical background" --ai-use none
aucove submit --summary "Background: Sepsis-3 criteria" --note-file ../note.json --ai-use none
```

`note.json` is their session note, a small file outside the project folder:

```json
{"done": "Wrote the clinical background with the Sepsis-3 criteria.", "next": "Add the criteria table."}
```

If they are asked for changes, they go back to the task with `aucove switch 30`, fix it, save and submit again. They review and approve on the GitHub website, like anyone else. Their commits say "AI-Use: none"; without that option they would be recorded as "undeclared".

</details>

<details>
<summary>Someone leaves the project</summary>

Before their access is removed, their evidence is exported: their copy of the seals, the records of their submissions and their parts. With that, they can later prove what they did and when.

</details>

<details>
<summary>There is a priority dispute</summary>

An arbitrator checks, without installing the project, that a file is exactly the one that was sealed on a given date: they use `aucove evidence verify` with that day's record and that file's opening. The rest of the work stays hidden.

</details>

## If something fails

<details>
<summary><code>aucove doctor</code> says there is no token, or that GitHub rejected it</summary>

The token is missing, expired or was deleted. Create a new one with the steps in [Your GitHub token](researcher.md#your-github-token), put it in `GH_TOKEN` and reopen the terminal and your agent.

</details>

<details>
<summary>The token is new, but GitHub says you do not have permission</summary>

Most likely the organization has not approved it yet. The lead approves it in the organization: Settings → Personal access tokens → Pending requests. If that is not it, check that the token includes the project repo.

</details>

<details>
<summary>The agent does not see the project context</summary>

Run `aucove doctor` in the terminal before editing: it says what is missing (the installation, the token or the permissions) and how to fix it. Also check that you opened the agent inside the project folder.

</details>

<details>
<summary>aucove is not installed or the version does not match</summary>

The agent stops and asks you to install it with the command from the guide. It does not install it on its own.

</details>

<details>
<summary>The agent sees a different version of aucove than your terminal, or cannot find it</summary>

On Windows, Claude Desktop is a packaged app: whatever is installed from inside it, by the agent or from its Terminal panel, stays in a private copy that only that app sees and that hides the one you install from PowerShell. `aucove doctor`, run from PowerShell, tells you whether it exists and where. To remove it:

1. Quit Claude Desktop from its icon in the system tray.
2. In a PowerShell opened from the Start menu, check whether it exists; if it shows nothing, there is no copy:

   ```powershell
   Get-Item "$env:LOCALAPPDATA\Packages\Claude_*\LocalCache\Roaming\uv\tools\aucove"
   ```

3. If it exists, delete it with `Remove-Item -Recurse -Force` followed by the path that step 2 showed, in quotes.
4. Open Claude Desktop and ask the agent to run `aucove --version`: it must show the same version as your PowerShell.

From then on, install and update aucove only from PowerShell.

</details>

<details>
<summary>The sealing says that a merge was left without the project signature</summary>

The job summary, a yellow warning in Actions and the comment on the submission say so, with the reason. The record was sealed anyway, because the date comes first, and the signature of the milestone index covers it later. So that the next ones are signed:

- `no_secret`: the secret `AUCOVE_EVIDENCE_KEY` is missing from the `aucove-evidence` environment. Add it with [step 3](project-signature.md#3-with-ci-mode-keep-the-key-on-github).
- `key_mismatch`: the key in the secret is not a current key of `project.yaml`, for example because you changed it in one place and not in the other. Check that both match.
- `sign_failed`: the secret is not an `ssh-ed25519` key without a passphrase. Upload it again without the passphrase.

</details>

<details>
<summary><code>init</code> stops because the project key is a secret of the repo or of the organization</summary>

A workflow on any branch can read a secret of the repo or of the organization, so the key cannot be there. Delete it on GitHub (Settings → Secrets and variables → Actions), put it in the `aucove-evidence` environment ([step 3](project-signature.md#3-with-ci-mode-keep-the-key-on-github)) and run `aucove init` again. If the key was exposed, change it.

</details>

<details>
<summary><code>init --repo</code> says that the folder or the repo is not empty</summary>

`init --repo` only creates the project in an empty folder and for an empty GitHub repo, so that it overwrites nothing. If the folder has files, use another one or empty it; system files (`.DS_Store`, `Thumbs.db` and `desktop.ini`) do not count. If the repo already has commits, for example because you created it with a README, delete it and create it again with nothing in it. If it already has the project, clone it and run `aucove init`, without `--repo`, inside the clone.

</details>

<details>
<summary><code>init --repo</code> says that git does not have your credentials, or GitHub rejects the push</summary>

`init` pushes the project with the GitHub credentials that git has already stored, without asking for them. If the error says `git_credentials`, git does not have yours: run `git ls-remote https://github.com/<organization>/<repo>.git` once in your terminal, which asks you to sign in, or `gh auth setup-git` if you use GitHub CLI, and run `init` again. If GitHub rejects the push with `permission` or `workflow_permission`, those credentials cannot push to the repo or create workflows: sign in with an account or a token that can. In both cases `init` leaves the folder empty, so it is enough to run it again.

</details>

<details>
<summary><code>init --repo</code> says that git does not have your identity</summary>

git needs your name and email for the `init` commit, and it does not have them for your account: perhaps you set them only inside another repo. Set them with your `noreply` address ([step 2 of Researcher](researcher.md#2-install-git-your-agent-and-aucove)) and run `init` again: the folder is still empty.

</details>

<details>
<summary>You lost the project key, or it was stolen</summary>

Give the key an end date and create another: [Changing the key, or revoking a stolen one](project-signature.md#changing-the-key-or-revoking-a-stolen-one). What was signed and sealed before that date stays valid.

</details>
