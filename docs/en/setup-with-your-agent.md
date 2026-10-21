# Setting up aucove with your agent

[User guide](README.md) › Setting up with your agent

Your agent can walk you through the README's [initial setup](../../README.md#initial-setup): it asks which step you are on, checks what you already did, tells you what comes next for your role and explains each error. Here are the messages you paste to it, in order. They work with any agent that can run commands and open a web page, such as Claude Code, Codex or Gemini CLI.

## What you do yourself

The agent does not do these steps, on purpose. It tells you when and how, and you do them:

- **The clicks on GitHub:** creating the organization, the repo and your tokens.
- **Installing git, Git Credential Manager, uv and aucove**, in a terminal opened from the Start menu. On Windows, whatever is installed from inside the agent's app stays in a copy that only that app sees.
- **Storing your token.** Never paste it into the chat: the agent only checks that it exists, without showing it.
- **Running `aucove init`**, in a terminal or in the Terminal panel of Claude Desktop. It asks for typed confirmations and the administration token, which must not go through the AI.

The agent does check, with commands that only read, your git identity, the aucove version, whether `GH_TOKEN` exists and, once there is a project, `aucove doctor`.

## Where to open it

Open it in the folder where you keep your projects, for example `research`, not inside the project folder: `init` needs an empty folder, and the agent's app may store its own files there.

The app only sees what you install or store before opening it. So, every time you install something or store your token, quit it completely and open it again in the same folder; quit Claude Desktop from its icon in the system tray. The agent reminds you. On macOS, Claude Desktop does not take `GH_TOKEN` from `~/.zshrc`: also store it in its local environment editor, as step 6 of the README says.

## 1. During the setup

Paste this message every time you open the agent to set up, with your role and the project repo in place of what is between `< >`. The agent asks which step you are on and goes on from there.

```text
I am setting up aucove (https://github.com/aucove/aucove) for the project <organization>/<repo>, as the <lead or a researcher>. Walk me through the README's initial setup, step by step and in order: https://github.com/aucove/aucove#initial-setup
- Ask me which step I am on. Before each step, ask me whether I already did it and, if possible, check it with commands that only read.
- Do not install anything, do not change the git configuration and do not create files: tell me what to do and I do it in my terminal.
- Never ask me for the token or show its value. For GH_TOKEN, only check whether it exists.
- When I install something or store the token, remind me to quit this app completely and open it again, because you will not see it before that. When I come back, I will paste this message again.
- I run aucove init myself in a terminal. You tell me when and explain its questions.
```

If your agent cannot open web pages, also paste it the README's "Initial setup" section.

This is how it starts, for example, with Laura as the lead:

> **Laura:** *(pastes the message)*
>
> **Agent:** Which step are you on? Do you already have a GitHub account with "Keep my email addresses private" turned on?
>
> **Laura:** Yes. I also created the `labfe` organization, but not the repo.
>
> **Agent:** Then let's do the empty repo: in the organization, Repositories → New repository, named `ising-diluido` and Private, with no README, no `.gitignore` and no license. Tell me when it is there.
>
> *They go on step by step. After installing aucove and storing the token, Laura quits the app, opens it again and pastes the message once more.*
>
> **Agent:** git has your name and your `noreply` address, `aucove --version` shows the README's version and `GH_TOKEN` exists. Go on with step 7: create the administration token and, in a terminal, run `aucove init --repo labfe/ising-diluido --dry-run`. If something fails, paste me what it says.

## 2. If something fails

Paste what the terminal said. No aucove output shows your token.

```text
This is what the terminal said when I ran <the command>. What does it mean and what do I do?
<paste the output here>
```

## 3. After `init` (lead)

Open another session of the agent, now inside the project folder, so it loads the project's rules and tools. Paste:

```text
Check with aucove doctor that everything is ready. If it is, prepare the project with me in task 1, one question at a time.
```

The agent checks your environment and interviews you, one question at a time, to complete the plan, the people and the parts; you answer and it writes the files, as in [Lead › Open your agent and prepare the project](lead.md#3-open-your-agent-and-prepare-the-project).

## 4. Telling your team (lead)

```text
Draft the message telling my team that the project <organization>/<repo> is ready. They should accept the GitHub invitation and follow aucove's initial setup as researchers (https://github.com/aucove/aucove#initial-setup), on their own or with their agent (https://github.com/aucove/aucove/blob/main/docs/en/setup-with-your-agent.md).
```

When their tokens show up as pending, approve them in the organization: **Settings** → **Personal access tokens** → **Pending requests**.

## 5. Your first session (researcher)

Once you have cloned the project, in step 7, open another session of the agent inside that folder and paste:

```text
Check with aucove doctor that everything is ready and tell me which parts and tasks are mine.
```

From there, follow [a day of work](researcher.md#a-day-of-work-in-physics).

## 6. When your token expires

```text
My aucove token expired. Walk me through creating another one and storing it in GH_TOKEN, with steps 5 and 6 of https://github.com/aucove/aucove#initial-setup, without asking me for the token.
```
