# The project signature

[User guide](README.md) › The project signature

The timestamps prove **when** each submission existed and that it did not change. The project signature also proves **whose** record it is: that your project issued it, and not someone who put together a similar folder. Whoever reviews the work from outside (a referee, a journal or a committee) checks it with the public key that the group publishes, without trusting GitHub, aucove or you.

Whoever administers the repo sets it up once; it is almost always the lead. It takes about 15 minutes.

> [!NOTE]
> The project signature is available since version 0.1.0rc2.

## What is signed and who keeps the key

| Your repo | Mode | What is signed | Where the private key lives |
|---|---|---|---|
| Public, or private with GitHub Team | `ci` | Every merge, on its own, in CI; and the index of each milestone | On your computer and in a protected GitHub secret |
| Private with GitHub Free | `admin` | The index of the package of each milestone | Only on your computer |

GitHub Team is free for teachers and researchers with GitHub Education. Submissions (what is sealed on each PR) are not signed in any mode, on purpose: their job gets no secrets. The signature of the milestone index covers them.

## 1. Create the key

In your terminal. The key is created on your computer and never goes into the repo:

```bash
ssh-keygen -t ed25519 -C "signature of ising-diluido" -f $HOME/.ssh/aucove-ising-diluido
```

It asks for a passphrase: choose a good one and keep it in your password manager. Two files are left:

- `aucove-ising-diluido`, the **private key**. It is the only one that signs: do not share it or upload it anywhere, and keep an encrypted backup, for example in your password manager.
- `aucove-ising-diluido.pub`, the **public key**. It only verifies, so that one is published.

The fingerprint identifies the key in a few characters:

```text
PS C:\Users\laura> ssh-keygen -lf $HOME/.ssh/aucove-ising-diluido.pub
256 SHA256:8jmj+t4ewK6CHVVPTPUNQTmLHKs6vyRBp9mxvEH7WXY signature of ising-diluido (ED25519)
```

Do not load this key into `ssh-agent` on a computer where AI agents run: that way nothing signs unless you type the passphrase.

## 2. Add the public key to the project

It goes in `project.yaml`. Ask your agent in a task of yours, for example the setup task: "add the project signature in ci mode with this public key", and paste the contents of the `.pub` file. It ends up like this:

```yaml
evidence_signing:
  mode: ci
  keys:
    - id: key-2026
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve signature of ising-diluido"
      since: "2026-10-15T00:00:00Z"
```

`id` is a short name for the key and `since` is when it starts being valid: today, in UTC. With a private repo on GitHub Free, the mode is `admin`. Once merged, the change is sealed like any other.

## 3. With `ci` mode: keep the key on GitHub

`init` already created the `aucove-evidence` environment, which only the base branch can use. It still needs the private key as a secret. CI cannot type the passphrase, so the secret goes without it: you make a temporary copy, remove the passphrase, upload it and delete it. In Git Bash (Windows) or in your terminal:

```bash
cp $HOME/.ssh/aucove-ising-diluido key-ci
ssh-keygen -p -f key-ci -N ""
```

`ssh-keygen -p` asks for the current passphrase. Then, on GitHub: the repo → Settings → Environments → `aucove-evidence` → Add environment secret, with the name `AUCOVE_EVIDENCE_KEY`, and paste the whole content of `key-ci`. When you are done, delete the copy:

```bash
rm key-ci
```

If `init` left adding this secret as a pending step, this is that step. The key never goes as a secret of the repository or of the organization: a workflow on any branch can read those, and `init` refuses to go on if it finds the key there.

## 4. Check

`aucove doctor` checks the mode, that a key is valid today and, with `ci`, the environment:

```text
[ok] evidence_signing: Project signature: mode `ci`; current keys: key-2026. The environment `aucove-evidence` only allows the branch `main`.
```

On the next merge, the summary of the sealing job says "Signed by the project with key `key-2026`". If it says that the record was left unsigned, the same summary explains why ([When something happens](when-something-happens.md#if-something-fails)).

## 5. Publish the public key

So that someone outside can verify without trusting you, the list of keys also lives outside the project, in a public repo of the group archived on Zenodo. You do it once:

1. On GitHub, create a public repo, for example `labfe/keys-ising-diluido`.
2. Upload three files to it:
   - `keys.yaml`, the same list as in `project.yaml`:

     ```yaml
     keys:
       - id: key-2026
         public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve signature of ising-diluido"
         since: "2026-10-15T00:00:00Z"
     ```

   - `allowed_signers`, to verify with `ssh-keygen` alone. It has one line per key, with the `since` date written without dashes or colons:

     ```text
     key-2026 namespaces="aucove-evidence",valid-after="20261015000000Z" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve
     ```

   - `README.md`, with the name of the group and of the project, and the fingerprint of each key (`SHA256:…`).
3. Archive it on Zenodo as in [Publishing](publish-and-safeguard.md#5-get-the-doi-from-zenodo): turn the repo on in your Zenodo profile and create a Release. The DOI dates the list and ties it to the members of the group with their ORCID.

Every time the list changes, update the three files and create another Release.

## 6. At each milestone, sign the package index

In both modes, when you put together the evidence package of a milestone, you sign it yourself with one command: [Publish and safeguard, step 6](publish-and-safeguard.md#6-sign-the-package-index). That signature covers everything that CI did not sign.

## Changing the key, or revoking a stolen one

A key is never removed from the list: that way, what it signed before still verifies.

- **To change it** (for example, every year or when the administrator changes): create a new key (step 1) and add it with today's `since`. Give the old one an `until` with the same date. With `ci` mode, replace the secret (step 3). Finally, update the published list (step 5).
- **If it is stolen or lost**: give it an `until` with the date from which it is no longer trustworthy (the date of the theft, if you know it), create a new one and update everything the same way. What was signed and sealed before that date stays valid, because the timestamp proves it already existed; what someone signs later with the stolen key does not verify.

```yaml
    - id: key-2026
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve signature of ising-diluido"
      since: "2026-10-15T00:00:00Z"
      until: "2027-10-15T00:00:00Z"
```

What the signature proves, and what it does not: it proves that the record was issued by whoever holds the project key. It does not prove who wrote each change; the authorship agreement and the history of the submissions tell that.
