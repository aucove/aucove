# Article guide

The article editor writes it (the lead, if `roles.yaml` declares no other editor).
Every session reads it before writing, so that the article reads as a single text.
If a part does not fit what this guide says, discuss it with the editor in the task.
Keep it short, with one sentence per line: it goes into the context of every session and should not exceed 16 KiB.

## Notation and terminology
- The notation is in `tex/notation.tex`; only the usage conventions go here.
- (List the project terms here and the form chosen for each one.)

## Tone and length
- (Define the tone, the grammatical person and the approximate length of each section here.)

## Article sections
Each section is a file in `tex/outline/` that includes its parts in order.

### Introduction (`tex/outline/introduction.tex`)
- What it covers: the problem, the motivation, the research questions and the contributions.
- Parts: `introduction`.

### Background and related work (`tex/outline/background.tex`)
- What it covers: the context of the problem and the related work, and how this work differs from it.
- Parts: `background`.

### Theoretical framework (`tex/outline/theory.tex`)
- What it covers: the concepts, definitions and models the work relies on.
- Parts: `theory`.

### Proposal and methodology (`tex/outline/methodology.tex`)
- What it covers: the proposal, the data, the procedures and the design decisions.
- Parts: `methodology`.

### Experiments (`tex/outline/experiments.tex`)
- What it covers: the design, the setup and the execution of the experiments.
- Parts: `experiments`.
- If several people write it, it is split into subparts, such as `experiments-a` and `experiments-b`, and this guide says what each one covers.

### Results analysis (`tex/outline/results.tex`)
- What it covers: the results of the experiments, with their figures and tables, and their interpretation.
- Parts: `results`.

### Conclusions (`tex/outline/conclusions.tex`)
- What it covers: the conclusions, the limitations and the future work.
- Parts: `conclusions`.
