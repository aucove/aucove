# Researcher

[User guide](README.md) › Researcher

What you do: set up your computer once and, after that, talk with your agent.

> [!NOTE]
> This describes the complete version 0.1. Shortcuts such as `/start` or `/submit` arrive with 0.1.0 for Claude Code and Codex. With 0.1.0rc5, or with another agent, you ask for the same thing in your own words, for example "let's start with task 8" or "submit the task", or use the `aucove` commands.

## Before the first day

You do this once. There are two ways to work:

- **On your computer.** This is the usual way. Follow the README's [initial setup](../../README.md#initial-setup) as a researcher: you accept the invitation, install git and aucove, create your token, clone the project and open your agent in that folder. Whatever the agent runs, such as your analysis scripts, runs on your computer and with your data. If you would rather have your agent with you, use [Setting up aucove with your agent](setup-with-your-agent.md).
- **From the browser.** You go to claude.ai/code and pick the project repo. The session installs aucove by itself, so you install nothing and create no token: steps 1 and 2 of the initial setup are enough. Whatever the agent runs, runs on a cloud machine that only sees the repo. You need a Claude plan that includes Claude Code on the web (Pro, Max or Team; on Enterprise, a seat with Claude Code), and the lead must have installed the Claude app on the repo.

The difference is in [How it works › Where the code runs](how-it-works.md#where-the-code-runs).

### Your first session

Open your agent in the project folder:

```bash
claude
```

Or your agent's command, such as `codex`, `copilot` or `gemini`; or open the folder in your editor, or pick it in the Code tab of Claude Desktop. Ask it to run `aucove doctor`. When everything is ready, it looks like this (abridged):

```text
PS C:\Users\dramirez\research\ising-diluido> aucove doctor
Core 0.1.0 · C:\Users\dramirez\research\ising-diluido · profile enforced
[ok] identity: @dramirez, with a fine-grained token from `GH_TOKEN` (this repository only).
…
```

If something is missing, each line says what and how to fix it, for example "[failed] identity: There is no forge token…" with the link to create it. Then, the first time you type `/start`, the agent gives you a short welcome: your parts, your tasks and how the work goes.

## A day of work in physics

Diego is a postdoc. With Laura, the lead, he is writing a paper on the critical exponents of the diluted Ising model, and he is in charge of the methods and the results. He works on his laptop with Claude Code.

> *Diego opens the terminal in the project folder and types `claude`. When it starts, the agent already knows who Diego is, which parts are his and who is working on what.*
>
> **Diego:** `/start`
>
> **Agent:** You have two open tasks in your parts: 8, "Methods for the diluted Ising model", and 9, "Wolff algorithm". Laura is on the introduction. Which one shall we start with?
>
> **Diego:** 8.
>
> **Agent:** I need your permission to claim it.
>
> *Diego accepts in the permission prompt.*
>
> **Agent:** Task 8 is yours. I have read the plan: the main question, RQ-1, is whether the exponents follow strong or weak universality.
>
> **Diego:** Look for papers from 2010 to 2025 on the critical exponents of the 2D diluted Ising model. Make me a table with the method, the lattice sizes, the value of ν and whether they support strong or weak universality.
>
> **Agent:** I found 14 papers on arXiv and in journals. The table is in `sections/methods/literature.md`, with the DOI or arXiv number of each one. They disagree: seven support strong universality, five support weak universality and two are inconclusive.
>
> **Diego:** Why do you think they disagree? What could we contribute?
>
> **Agent:** They differ in three things: the lattice sizes, how they handle logarithmic corrections and how many disorder realizations they average. We could contribute larger lattices, a fit that separates the logarithmic corrections, or a comparison between site and bond dilution. That last one is outside the scope of the plan (OUT-1).
>
> **Diego:** Larger lattices are already task 9. Develop the fit: what form would you use, and who has used it?
>
> *They discuss the form of the fit for a good while. Diego corrects one of the agent's assumptions, and they settle on a version with one parameter fewer.*
>
> **Diego:** Write a script that plots ν against the dilution concentration with the values from the table and their error bars.
>
> **Agent:** I wrote `sections/methods/analysis/nu_vs_p.py` and ran it. The figure is in `sections/methods/figures/nu-vs-p.pdf`, recorded in `provenance.yaml` with the script that produced it. I saved the work.
>
> *The script ran on Diego's laptop, with his Python and his files.*
>
> **Diego:** Draft the methods paragraph that justifies the fit and cite the three main papers.
>
> **Agent:** Two of those papers are not in the group's bibliography. Add them to Zotero with these DOIs and paste what you export into `pending.bib` in your part. Meanwhile, I will leave the paragraph with those two citations marked.
>
> *Diego adds them to Zotero by their DOI and pastes the export. The agent completes the citations.*
>
> **Diego:** `/submit`
>
> **Agent:** I wrote the session note: what we searched for and with what criteria, why we dropped the site-versus-bond comparison, and that the fit still has to be tested with our data. I compared your submission with the other active tasks and I see no clashes. To open the submission I need you to confirm this declaration: "I declare that I have personally reviewed the content of this submission, including any parts produced with AI assistance; that the AI-use declaration is accurate and complete; and that I take responsibility for this content before the project team." Do you confirm it?
>
> *Diego reads it and confirms it. The submission is opened on GitHub as a PR, and CI checks it, builds the PDF and leaves the seal:*
>
> **Submission seal** · PR #14 · task 8 · 2026-11-18, 17:42 UTC
>
> *Laura gets the notification, reviews the submission on GitHub and merges it. If she asked for changes, Diego would come back with `/start 8`, fix it and submit again.*

## A day of work in machine learning

Pablo is a PhD student at another university. With Valeria, the lead, he is building an open benchmark of small language models, and he is in charge of the experiments and the results. He works with Codex on a workstation with a GPU.

> **Pablo:** `/start 7`
>
> **Agent:** Task 7, "Context length ablation", is yours. A heads-up: Valeria has task 6 active, which changes the test split. If it is merged before yours, your numbers will change.
>
> **Pablo:** Before writing code: which lengths would you try, and with how many seeds? I have a 24 GB GPU and I want everything to run overnight.
>
> **Agent:** 128, 256 and 512 tokens with 5 seeds fit in about 6 hours with the current batch size. 1024 does not fit in memory without lowering the batch size; I would leave it for another task.
>
> **Pablo:** Agreed. Add the `--max-len` option to `train.py` and a script that launches the 15 runs.
>
> **Agent:** Done, they are in `sections/experiments/analysis/`. It is best if you launch the long runs yourself, outside this session.
>
> *Pablo launches the script on his workstation before leaving: the 15 runs use his GPU, not the cloud. The next morning:*
>
> **Pablo:** Gather the results: mean and standard deviation of the F1 per length, in a table and a plot.
>
> **Agent:** The table and the plot are in your experiments part, recorded in `provenance.yaml`. The trained models weigh 300 MB each and do not go into the repo: they stay on your disk or on Hugging Face, and Valeria notes where they are in `data/README.md`.
>
> **Pablo:** What conclusion would you draw?
>
> **Agent:** The F1 goes up from 128 to 256 tokens. Between 256 and 512 the difference is smaller than the variation between seeds, so I would not call it real.
>
> **Pablo:** Write it like that in the results, with the table.
>
> **Pablo:** `/submit`
>
> **Agent:** Comparing your submission with the active tasks, I see a possible clash with Valeria's task 6: if the split changes, these numbers change. I am leaving it as a warning on the submission so she can decide.
>
> *Valeria merges her task 6 first. Pablo asks the agent to "update", reruns the script and submits again. The second submission adds up.*

## What you type or say

| In the agent's chat | What happens |
|---|---|
| `/start` | Shows your tasks, claims the one you choose (or proposes a new one) and reads the plan. |
| Whatever you need | Search, read, discuss ideas, write, code and run code. |
| `/submit` | Writes the session note, looks for clashes with other tasks, asks you to confirm the responsibility declaration and opens the submission for review. |
| "save" | Optional: the agent saves on its own at the end of each block of changes. |
| "update" | Brings into your task what is new in the shared version. The agent suggests it when needed. |
| `/project-status` | What is open, who is doing what and what is waiting for review. |
| `/decision` | Records a group decision, such as a notation convention. |

Anything that starts with `aucove`, such as `aucove doctor`, is a terminal command: you type it in PowerShell or in the Terminal, not in the chat. In Claude Code you can run a command from the chat if you start it with `!`, for example `! aucove`.

## What gets recorded

- **In the repo:** your files (text, tables, code and figures); every save, with the AI tool and model you used; the note of each submission; and the decisions.
- **Outside the repo:** your conversation with the agent, which stays in your account or on your computer; the raw data, which `data/README.md` describes; and files larger than 5 MiB.
- **Without you doing anything:** the seal of each submission and of each merge.

## What the agent does not do

- Touch other people's parts. If something needs to change there, it helps you ask its owner.
- Approve, accept tasks or merge. A person decides that.
- Make up references. Citations come from the group's bibliography.
- Upload personal data, credentials or raw data.
- Install or update aucove.
