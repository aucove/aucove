# Publish and safeguard

[User guide](README.md) › Publish and safeguard

What the lead does when the group closes a stage: a preprint, a journal submission, a thesis or the end of the project. We call those moments milestones.

While the group works, the seals happen on their own and stay on GitHub. But GitHub deletes its CI records after a while (400 days in a private repo, 90 in a public one), and what is in a private repo is neither public nor citable with a DOI. That is why, at each milestone, the lead does two jobs:

| Job | What for | When | In v0.1 |
|---|---|---|---|
| The seals | Prove what existed and since when | On every submission and merge | Automatic |
| Safeguarding the evidence | Make the seals outlive GitHub, kept in two places | At each milestone and at the end | By hand |
| Publishing | Open the work and give it a DOI | When the group decides | By hand |

Version 0.2 will do these steps with `aucove evidence export`, `deposit` and `disclose`. In v0.1 they are done with `git` and GitHub CLI.

## Safeguarding the evidence

You put together a package with everything that proves what was submitted and when, and keep it outside GitHub. You need git, [GitHub CLI](https://cli.github.com) (`gh`) and your everyday token, which `gh` takes from `GH_TOKEN`. On Windows, these commands go in Git Bash, which comes with git.

### 1. Create a folder for the package

Outside the project folder: the package never goes into the repo. Put your repo's name in `R`.

```bash
mkdir evidence-milestone1 && cd evidence-milestone1
R=labfe/ising-diluido
```

### 2. Download the sealing records

Each seal is a folder that CI keeps as an "artifact". This command downloads those of every run, before GitHub deletes them.

```bash
for id in $(gh run list -R $R --workflow seal.yml --limit 500 --json databaseId --jq '.[].databaseId'); do
  gh run download $id -R $R --pattern 'evidence-*' --dir artifacts/$id
done
```

### 3. Keep the seal comments

CI also leaves each seal as a comment on its submission. They are all saved to one file.

```bash
gh api --paginate repos/$R/issues/comments \
  --jq '.[] | select(.body | contains("aucove:ci:evidence"))' > comments.jsonl
```

### 4. Copy the full history

The whole history of the repo in a single file, including the submissions that were not merged.

```bash
git clone --mirror https://github.com/$R.git repo.git
git -C repo.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
git -C repo.git bundle create ../repo.bundle --all
rm -rf repo.git
```

### 5. Compute the fingerprint of each file

A list with the SHA-256 fingerprint of everything in the package.

```bash
find . -type f ! -name index.txt -print0 | xargs -0 sha256sum > index.txt
cd ..
```

### 6. Check that the records are sound

Optional. It checks each record in the package and gives a verdict per record.

```bash
aucove evidence verify evidence-milestone1/artifacts
```

Each record carries its own `VERIFY.md`, so anyone can verify it later, even without aucove.

### 7. Keep it in two places outside GitHub

For example, your institution's data repository, with closed access, and your own copy. Zenodo does not work for this, because what you upload there is public.

<details>
<summary><b>If the repo is private and there is no institutional repository:</b> an encrypted deposit on OSF</summary>

The package is encrypted for two people (you and the institution, or you and the deputy), each of whom keeps their own key. You need the [age](https://github.com/FiloSottile/age) program.

1. Each recipient creates their key once with `age-keygen -o age-key.txt`, keeps it somewhere safe and sends you its public part, which starts with `age1`.
2. Pack and encrypt:

   ```bash
   tar -cf evidence-milestone1.tar evidence-milestone1/
   age -r age1laura… -r age1diego… -o evidence-milestone1.tar.age evidence-milestone1.tar
   sha256sum evidence-milestone1.tar.age
   ```

3. On osf.io, create a new registration (OSF no longer creates new projects, but it does create registrations) with a neutral title, such as "Evidence package, milestone 1", upload only the `.age` file and choose an embargo of up to 4 years. Since it is encrypted, nothing is revealed when the embargo ends.
4. Schedule a review before the embargo ends, to renew it or decide what to do.

</details>

### 8. Record the safeguard in a decision

In your agent, with `/decision`: the date, where the package ended up (and, if you used OSF, the registration identifier and the end of the embargo) and the SHA-256 fingerprint of the file you kept. That decision is sealed in the next merge.

## Publishing

Opening the work is a disclosure: before doing it, the group goes through the disclosure checklist it set in the authorship agreement. With a project open from the start, the repo is already public, and steps 4 and 5 are skipped.

### 1. Go through the disclosure checklist and record it

In your agent, with `/decision`, in this order:

- if something may be patentable, the application is filed first (in Mexico, with the IMPI), because publishing is disclosing;
- personal data and the ethics approval;
- the agreement with the funder and the institution's ownership;
- the journal's policy on preprints;
- the agreement of all the authors;
- that every commit uses GitHub's private (noreply) email addresses.

### 2. Mark the version with a tag

A git tag marks the exact state that is published. In your terminal, inside the project folder:

```bash
git switch main
git pull
git tag -a v1-preprint -m "Version submitted to arXiv"
git push origin v1-preprint
```

### 3. Upload the preprint

Use the PDF that CI built for that commit: on GitHub, Actions → the run of that commit on `main` → Artifacts. Upload it to arXiv, medRxiv or another server.

### 4. Create the public mirror

A new public repo with the history up to the tag, without Issues or comments, because the seal comments hold data that must stay private. First create the empty repo on GitHub (New repository, Public, no README); here it is called `ising-diluido-v1`. Then:

```bash
git clone --branch v1-preprint --single-branch https://github.com/labfe/ising-diluido.git mirror
cd mirror
git remote set-url origin https://github.com/labfe/ising-diluido-v1.git
git push origin HEAD:refs/heads/main
git push origin v1-preprint
```

### 5. Get the DOI from Zenodo

1. On zenodo.org, log in with your GitHub account.
2. In your profile, open the GitHub section and turn on the switch for the mirror.
3. On GitHub, in the mirror, create a Release with the `v1-preprint` tag (Releases → Draft a new release → choose the tag → Publish release).
4. Zenodo archives it within minutes and gives it a DOI. It also gives a concept DOI, which always points to the latest version.

### 6. Archive the code in Software Heritage

On archive.softwareheritage.org, open "Save code now", paste the mirror's address and submit it. It is archived long term with its own identifier.

### 7. Cite the DOI and record it

The DOI goes in the data and code availability section of the paper. Record the tag, the DOI and the mirror's address with `/decision`.
