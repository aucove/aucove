# Verifying a record

[User guide](README.md) › Verify

For whoever receives a record or an evidence package and wants to check it on their own: a referee, a journal, an ethics committee or someone from the same group. You need no GitHub account, and you do not have to trust the group, GitHub or aucove.

A **record** is a folder such as `integration-<commit>/`: CI put it together when it sealed a submission or a merge. A **package** is the folder that the lead puts together at each milestone, with many records, an `index.txt` and, if the project signs, a `signature/` folder.

Each record and each signature folder carries its own `VERIFY.md`, with the exact steps for that folder. This page sums them up.

## What is checked

| Question | How it is answered |
|---|---|
| Did it exist on that date? | The RFC 3161 timestamp of two time-stamping authorities (TSA) and the OpenTimestamps proof |
| Has it changed since? | The SHA-256 fingerprint of each file |
| Did the project issue it? | The project signature, with the list of keys that the group publishes |

The signature proves that the record was issued by whoever holds the project key. It does not prove who wrote each change: the authorship agreement and the history of the submissions tell that.

## With aucove

It is the fastest way. Install it with the `evidence` extra ([Install aucove](../../README.md#4-install-aucove)) and download the list of keys, `keys.yaml`, from the group's public repo or from its Zenodo record. Then:

```bash
aucove evidence verify integration-3f2a…/ --keys keys.yaml
```

The answer gives, for each record:

- the **verdict** on the date: `verified` if a valid RFC 3161 timestamp backs it, `pending` if it only has an unconfirmed OpenTimestamps proof, or `failed` if something does not match;
- the **origin**: `project` if a key of the project that was valid on the date of the timestamp signed it, `unknown` if there is a signature but it could not be checked (the list of keys or the date is missing), or `unsigned`.

With a whole package, the same command also checks the signature of the index and that each file matches its fingerprint:

```bash
aucove evidence verify evidence-milestone1 --keys keys.yaml
```

If you also have a copy of the repo (the package's `repo.bundle`, or a clone), `--repo <folder>` checks the commits; with the files of a submission, `--content <folder>` checks that they are the sealed ones.

## Without aucove

With common tools: `sha256sum`, `openssl` and `ssh-keygen` (the one in Windows 11 and the one in Git for Windows work). The `VERIFY.md` of each folder has the commands with their values; these are the three steps.

1. **The fingerprint.** `sha256sum manifest.json` must give the `manifest_sha256` of `record.json`.
2. **The signature**, if the folder has a `manifest.json.sig`. With the `allowed_signers` file that the group publishes and OpenSSH 9.1 or later:

   ```bash
   ssh-keygen -Y verify -f allowed_signers -I key-2026 -n aucove-evidence -s manifest.json.sig -O verify-time=20261006120000Z < manifest.json
   ```

   It must answer `Good "aucove-evidence" signature for key-2026`. `verify-time` is the date of the earliest RFC 3161 timestamp, which `VERIFY.md` already has written: that way you check that the key was valid when it was sealed. With an older OpenSSH, `ssh-keygen -Y check-novalidate -n aucove-evidence -s manifest.json.sig < manifest.json` checks the signature, and you compare the key's fingerprint and dates by hand with the published list.
3. **The timestamp.** With `openssl ts -verify`, as `VERIFY.md` says. In a signed record, the timestamp is of `manifest.json.sig`: the signature only verifies with those bytes of `manifest.json`, so the timestamp dates both.

In a package, the `signature/` folder is verified the same way, with `index.txt` instead of `manifest.json`, and then each line of `index.txt` with `sha256sum -c index.txt`.

## If something does not match

A `failed` says which check did not pass and why. The most common causes:

- the file changed after it was sealed, or it was copied with other line endings: the fingerprint does not match;
- the signature is from a key that is not in the published list, or from one that was no longer valid on the date of the timestamp;
- the folder is incomplete: a file of the timestamp or of the signature is missing. That check is left undone and, if no other timestamp is left, the record stays `pending`; it does not give `failed`, because a partial copy is not an alteration.

In any of those cases, ask the group for the original folder or for their milestone package.
