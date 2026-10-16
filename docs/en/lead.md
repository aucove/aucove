# Lead

[User guide](README.md) › Lead

You set up the project once and, every week, you review what the others submit.

> [!NOTE]
> This describes the complete version 0.1. With 0.1.0rc3, what is done here with a shortcut such as `/start` you ask the agent for in your own words, or do with the `aucove` commands.

## Setting up the project

You do this once, in eight steps and in this order. The first five happen on GitHub and in the terminal; from the sixth on, you work with your agent.

### 1. The space on GitHub

Create an organization for the group (on GitHub: your photo → Your organizations → New organization) and invite each person as a member (People → Invite member). For GitHub to enforce the rules on a private repo you need GitHub Team, which is free for teachers and researchers through GitHub Education. A public repo works with GitHub Free.

### 2. The authorship agreement

It is an ordinary document, a PDF or a Word file, that the group drafts and signs before starting. It is not an aucove file and it does not live in the repository, because it carries signatures and personal data: it is kept wherever the group keeps its documents, for example the institution's shared folder. It sets:

- the order and criteria of authorship of the paper, with each person's roles;
- when and how the work is published, and what is checked first (the disclosure checklist);
- who keeps the evidence and who stands in for them;
- each person's right to take their evidence with them if they leave;
- where and for how long the evidence package is kept.

Version 0.1.0 brings a model agreement in Spanish and English. In the meantime, the group drafts it with those points; your agent can help you with a draft, which does not go into the repo.

Only its **fingerprint** reaches the repo: 64 characters that identify the signed file without revealing its content. Compute it in your terminal:

```powershell
Get-FileHash .\signed-agreement.pdf -Algorithm SHA256
```

```bash
shasum -a 256 signed-agreement.pdf
```

The date and the fingerprint go into the line that `memory/constitution.md` has under "Integrity and ethics". You do not have to edit it yourself: in the interview of step 6 the agent asks you for both and fills in the line. The project's first seal covers that file, so there is a verifiable date showing the agreement existed, without publishing it. If anyone has doubts later, it is enough to compute the PDF's fingerprint and compare.

### 3. The empty repo

On GitHub, in the organization, create an **empty** repo: New repository, a name (for example `ising-diluido`) and Private, with no README, no `.gitignore` and no license. Do not add anything to it: in step 5, `aucove init --repo` pushes the whole project to it from your computer.

With 0.1.0 you will also be able to create the repo from the published template (`aucove/aucove-template`, "Use this template"), clone it and run `aucove init`, without `--repo`, inside the clone in step 5.

### 4. Your two tokens

You need the everyday token, like everyone else ([the steps are in Researcher](researcher.md#your-github-token)). For `init` you also need an administration token, used only that day:

- the same form, with the **Administration** permission on "Read and write", and **Secrets** and **Environments** on "Read-only", in addition to the others, and an expiration of 1 day;
- do not keep it in `GH_TOKEN`: you paste it when `init` asks for it, and it is neither shown nor stored;
- when `init` is done, delete it on GitHub.

As the organization owner, your tokens need no approval. Your team's do: you approve them in the organization, Settings → Personal access tokens → Pending requests.

### 5. `aucove init --repo`

It runs only once, in a regular terminal (PowerShell on Windows, Terminal on macOS), inside an empty folder that becomes the project folder. It does not run inside your agent, on purpose: it asks you to type confirmations and paste the administration token, and none of that should go through the AI. Your agent comes in at the next step.

```bash
mkdir ising-diluido
cd ising-diluido
aucove init --repo labfe/ising-diluido
```

`init` writes the template of the installed version in the folder, with your answers, and when you confirm, it creates the git repository and pushes it to the empty repo in a single commit. To push, git uses your GitHub credentials: if you have never pushed anything to GitHub from this computer, first run `git ls-remote https://github.com/labfe/ising-diluido.git`, which asks you to sign in, or `gh auth setup-git` if you use GitHub CLI.

> [!TIP]
> If you would rather have your agent with you from the start, open it in the empty folder before this step: it can help you install aucove and explain each question. You run `init` yourself in a separate terminal and, when it finishes, you restart the agent in the same folder so it loads the project's tools and your token.

Options: `--language en` to skip the language question, `--format none` for no LaTeX, `--disclosure open` for a project open from the start, and `--individual` for a one-person project.

This is what it looks like (abridged example; what you type comes after each question):

```text
PS C:\Users\laura\research\ising-diluido> aucove init --repo labfe/ising-diluido
Idioma del proyecto / Project language (es/en): en
Copyright holder (person, group or institution) [The ising-diluido authors]: Statistical Physics Lab
License for text (manuscript, plan.md, memory and everything else) [LicenseRef-AllRightsReserved]. Options: …: (Enter)
License for code [MIT]. Options: …: (Enter)
License for data [LicenseRef-AllRightsReserved]. Options: …: (Enter)
init will set up labfe/ising-diluido (enforced profile):
- Folder: C:\Users\laura\research\ising-diluido is empty; init creates the project and its git repository there, with the `main` branch and `origin` at https://github.com/labfe/ising-diluido.git.
- Project: language en, format latex, disclosure milestones, mode group.
- roles.yaml: you (lmendez) become the lead, the only person and the owner of every part; add your team later.
- Licenses: text LicenseRef-AllRightsReserved, code MIT and data LicenseRef-AllRightsReserved; holder: Statistical Physics Lab. …
- Files: 70 new, 0 changed and 0 deleted; they are pushed to `main` in one commit.
- GitHub: repository settings (squash only, with the PR title and body), Actions, collaborators and labels.
- Protections: the rulesets of the base, the task branches and the workflows, and their verification.
- Setup task: creates and accepts “Prepare the project”, yours and with every part, to complete the plan, the people and the parts with the agent.
Next I will ask for a short-lived GitHub administration token; it is neither shown nor stored.
To confirm, type exactly "init labfe/ising-diluido" and press Enter: init labfe/ising-diluido
roles.yaml has no deputy: nobody can approve your PRs. If you confirm, you (lmendez) become a bypass actor …
To confirm, type exactly "bypass lmendez" and press Enter: bypass lmendez
GitHub administration token (Administration read and write, and Secrets and Environments read-only; neither shown nor stored): (you paste the token; it is not shown)
init on labfe/ising-diluido (enforced profile):
Files (70):
  created: .gitattributes
  created: .github/CODEOWNERS
  …
Labels created or updated: task, state:proposed, state:expired, …
  applied: repo_settings
  applied: ruleset:aucove-base
  …
The files were pushed to the base branch.
Pending:
  - Prepare the project in task #1, “Prepare the project”: open your agent in the project folder and ask it to guide you through the plan, the people and the parts.
Before working: everyone turns on the GitHub noreply email and signs the authorship and disclosure agreement (00-marco D-44 and D-49).
```

What it left behind: the project folder, with its git repository connected to the GitHub one, as if you had cloned it; GitHub configured (protections, labels, how long the evidence is kept and the `aucove-evidence` environment for the project signature); the project files in one commit, and your setup task, #1, already accepted. Now delete the administration token on GitHub.

If `init` fails before pushing the commit, it leaves the folder empty again: fix what the error says and run it again. If it fails later, for example while applying the protections, run the same command again in the same folder and it picks up where it stopped. [If something fails](when-something-happens.md#if-something-fails) covers the most common errors.

### 6. Open your agent and prepare the project

In the same folder, open your agent. There is nothing else to copy or install: the folder that `init` left already carries what connects the agent to aucove (`AGENTS.md`, `CLAUDE.md` and `.mcp.json`), and aucove is already installed on your computer. If your agent was already open in that folder, restart it: the project's tools and your token are read at startup.

```bash
claude
```

When it starts, the agent receives the "Setup pending" notice and offers to prepare the project. With 0.1.0 you type `/start`; with 0.1.0rc3, you ask in words: "prepare the project in task 1". It interviews you in plain language:

> **Laura:** `/start`
>
> **Agent:** The project has just been created: the plan is the template's and you are the only person. I am claiming your setup task, #1, and I will ask you a few questions. What is the project about, and what question do you want to answer?
>
> **Laura:** Monte Carlo simulations of the 2D Ising model with dilution. We want to know whether the exponents follow strong or weak universality.
>
> **Agent:** Who is taking part, and with which GitHub account?
>
> **Laura:** Diego Ramírez, dramirez. He is my deputy.
>
> **Agent:** How do you split the paper?
>
> **Laura:** Me, the introduction, the model and the conclusions. Diego, the methods and the results.
>
> **Agent:** Which AI account does each of you use?
>
> **Laura:** We both have Claude Pro.
>
> **Agent:** Pro is a consumer plan. In a private repo, aucove asks by default for accounts with commercial terms, such as Claude Team, under which the provider does not train models on your conversations. You can move to Team, or turn off training on both accounts and record the exception. Which do you prefer?
>
> **Laura:** We will turn off training.
>
> **Agent:** I am noting that you both use Pro without training and recording the decision. Does the data include information about people?
>
> **Laura:** No. They are simulations that live on the institute's cluster.
>
> **Agent:** Then the raw data stays on the cluster, and in `data/README.md` I note where it is and whom to ask for access. Do you have the date and the fingerprint of the authorship agreement?
>
> **Laura:** We signed it on November 2. The fingerprint is 3be1f0…
>
> *They go on with the writing style. At the end, the agent writes the files and checks them.*
>
> **Agent:** I wrote `plan.md`, `roles.yaml`, the constitution and `data/README.md`; I regenerated the files that come from `roles.yaml` and validation passes. Shall I submit task 1?

Laura did not need to know keys such as `account_type` or `data_policy`: the agent translated her answers. After the submission, she reviews the changes on GitHub and merges them with "Squash and merge"; while there is no deputy, GitHub lets her merge her own submission through the bypass she confirmed in `init`.

### 7. The first tasks

> **Laura:** Propose the first tasks from the plan.
>
> **Agent:** I propose three: "Method: Wolff and disorder averages" (methods part, Diego's; METH-1), "Results: Binder cumulant crossings" (results, Diego's; RQ-1) and "Introduction and model" (introduction and model, yours). Shall I create them?
>
> **Laura:** Yes.
>
> *The agent asks for permission for each one and opens tasks #2, #3 and #4 on GitHub.*
>
> **Agent:** They are proposed. For them to be claimable, you have to accept them: that is a decision the agent cannot make.

To accept a task on GitHub:

1. Open the repo and go to the **Issues** tab.
2. Open the task, for example #2.
3. On the right, under **Labels**, press the gear and uncheck `state:proposed`. The task is now open for its owner to claim.

Or in your terminal, with a typed confirmation:

```text
PS C:\Users\laura\research\ising-diluido> aucove task accept 2
You are about to accept task #2 “Method: Wolff and disorder averages”: the `state:proposed` label will be removed and the task will be open to claim.
To confirm, type exactly "accept 2" and press Enter: accept 2
```

### 8. Tell your team

Check that each person accepted the invitation to the organization and send them the [researcher guide](researcher.md): install, create their token, clone and open their agent. Approve their tokens when they show up in Pending requests. If someone will work from the browser, install the Claude app for GitHub on the repo.

When you are done, set up the [project signature](project-signature.md), which takes about 15 minutes: with it, whoever reviews the work from outside can check that your project issued each record, not only when it existed.

## Every week

- **Review submissions.** In your agent, `/reviews` summarizes each submission for you: what changed, the session note, the checks and the clash warnings. Then, on GitHub, open the submission under Pull requests, look at the "Files changed" tab and the link to the PDF that CI left, press "Review changes" → "Approve" and, finally, "Squash and merge". If something is not right, choose "Request changes" with a comment and the person fixes it in the same submission. The expected turnaround is 72 hours.
- **Nobody approves their own work.** Your submissions are approved by the deputy.
- **Accept tasks.** Anyone can propose tasks. You or the deputy accept them as in step 7.
- **Decisions and direction.** `/decision` records a group decision; a decision is never deleted, it is replaced by another one. `/direction` proposes a change to the plan; if you accept it, you edit `plan.md` in a task of yours.
- **If the project uses LaTeX.** Someone makes sure the paper reads as a single text: the editor, who is you unless the group names someone else. Their approval is needed on submissions that touch LaTeX, and `/edit-article` helps them find repetitions, gaps and inconsistent notation.

## The project board

There are two ways to see how the project is going. In a terminal, inside the project folder, type `aucove` alone, with nothing else: it prints a text board. You do not type it in the agent's chat; in Claude Code you can run it from the chat as `! aucove`.

```text
PS C:\Users\laura\research\ising-diluido> aucove
aucove 0.1.0

Project: Critical exponents of the diluted Ising model
Identity: @lmendez (lead)

Open tasks (1):
  #9 [open] Wolff algorithm (parts: methods)
Who works on what (2; * = yours):
  #8 [in review] @dramirez: Methods for the diluted Ising model (parts: methods)
 *#3 [claimed] @lmendez: Introduction and model (parts: introduction, model)
Parts and owners:
  introduction (Introduction): @lmendez
  model (Model): @lmendez
  methods (Methods): @dramirez
  results (Results): @dramirez
  conclusions (Conclusions): @lmendez
Seals of your submissions in `.aucove/evidence/` (1):
  submission-4f2a9c1…-a1

Next step: open your agent in this folder and use the `start` skill, or claim a task with `aucove claim <n>`.
```

In the agent's chat, type `/project-status` or ask in words. It reads the same data and answers with a summary:

> **Laura:** How is the project going?
>
> **Agent:** There is one open task, 9, and two in progress: Diego submitted 8 and it is waiting for your review, and you have 3 claimed. There are no clash warnings and the shared version builds. Shall I prepare the review of 8?

## What only a person decides

Setting up the repo with `init`, accepting or cancelling tasks, approving, requesting changes and merging. The agent can get everything ready, but the decision is yours or the deputy's.
