# Project constitution

## 1. Purpose of this document
This document gathers the principles, style and working rules of the project.
The research direction (questions, hypotheses, scope and methods) is in [plan.md](../plan.md), not here.
The lead maintains it and changes it only through a task of their own.

## 2. Integrity and ethics
- No `restricted` or `local_only` data is written in files, commits, Issues, PRs or notes.
- Raw data is not versioned: `data/README.md` only describes it and says how to obtain it.
- Every empirical claim rests on a cited source or on a reproducible analysis in the repository.
- If the project involves human participants, the ethics approval and informed consent are respected.
- Authorship and disclosure agreement, kept outside the repository: signed on (YYYY-MM-DD), with SHA-256 (paste here the hash of the signed file).

## 3. Writing style
- Language variant: (define the variant here, for example US English).
- Person and tense: (for example, first person plural and present tense for results).
- Terminology: one term per concept; technical terms are defined where they first appear.
- Units and numbers: International System of Units; (define the decimal and thousands separators here).
- One sentence per line in the `.md` files of the parts and in those of `memory/`.

## 4. Notation principles
- The notation of the project is defined in this section and only here: each symbol, with its meaning and its units.
- A symbol has a single meaning throughout the project.
- A new symbol is proposed in the PR and the lead adds it to this section.
- (Define the symbols of the project here, one per bullet.)

## 5. Use of AI
- Each person reviews everything they produce with AI and takes responsibility for it.
- Each PR declares the use of AI in its section and in its final block.
- AI is not listed as an author and does not appear in the credits.
- (Link here the AI use policy of your institution or of the target journal.)

## 6. Review criteria
- The change answers the task and the plan IDs it cites.
- The text is clear, follows the style of section 3 and cites its sources.
- Figures and tables have their provenance recorded and can be regenerated.
- The reviewer checks that every citation in the text identifies its source and that the links between files work.

## 7. Sources of truth
- Direction: [plan.md](../plan.md).
- People, roles and the owner of each part: [roles.yaml](../roles.yaml).
- Notation: section 4 of this document.
- Rules for AI agents: [AGENTS.md](../AGENTS.md).
- Decisions: [memory/decisions/](decisions/).
