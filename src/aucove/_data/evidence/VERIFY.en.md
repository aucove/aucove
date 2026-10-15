# How to verify this evidence record

This folder is a **proof of existence at a date** of commit `{{commit}}`. It is the `{{kind}}` record (`submission`: the head of a PR; `integration`: a push to the base branch), attempt {{attempt}}, of artifact `{{artifact}}`. This folder is all you need to verify it: no access to the repository is required.

## What it proves and what it does not

- **It proves** that the content described in `manifest.json` already existed when the stamps were requested and that it has not changed since. The verifiable date comes from the RFC 3161 time stamps and from Bitcoin, through OpenTimestamps; it does not come from the clock of whoever sealed it.
- **It does not prove** who the author or the owner of the content is. Authorship rests on the authorship agreement, on the CRediT statement and on the records of the forge.
<!-- signature -->
- **It is signed by the project:** key `{{key_id}}` (fingerprint `{{key}}`) signed `manifest.json`, and the stamps date that signature. The signature proves that the record was issued by whoever holds the private key of the project, not who wrote each change. Compare the fingerprint with the one the group publishes in its public repository or in its Zenodo record (point 4).
<!-- /signature -->
- `manifest.json` contains no files, paths or names, only salted hashes (commitments). `openings.json` holds the salts, paths and identities that open those commitments. It is **private**: share it only with whoever must verify.

## What is in the folder

| File | What it is |
|---|---|
| `manifest.json` | What was sealed, in its exact bytes. Its SHA-256 hash is `{{manifest_sha256}}`. |
| `record.json` | The index of the record: which proofs were requested and which were obtained. |
| `openings.json` | The private openings of the commitments. |
<!-- signature -->
| `manifest.json.sig` | The project signature on `manifest.json`: it is what the proofs stamped. |
<!-- /signature -->
| `{{sealed}}.ots` | The OpenTimestamps proof, if it was obtained. |
| `tsa/<id>.tsr` | Each RFC 3161 stamp, exactly as the time-stamping authority (TSA) returned it. |
| `tsa/<id>.chain.pem` and `tsa/<id>.root.pem` | The certificate chain of the TSA and the root it was checked against when sealing. |
| `tsa/<id>.ocsp.der` or `tsa/<id>.crl.der` | The revocation status of the TSA certificate, captured when sealing. |
| `open_commitments.py` | A short program that opens the commitments with Python 3.8 or later. |

## 1. With a single command

With [uv](https://docs.astral.sh/uv/) installed, from this folder:

```sh
uvx --from "{{name}}[evidence]=={{version}}" {{name}} evidence verify .
```
<!-- signature -->

To check the signature as well, add the list of keys that the group publishes (a YAML file with the key `keys`):

```sh
uvx --from "{{name}}[evidence]=={{version}}" {{name}} evidence verify . --keys <list>
```

With it, the answer gives the origin of the record: `project` if a key of the project that was valid at the date of the stamp signed it. Without it, the origin stays `unknown` and the fingerprint must be compared by hand.
<!-- /signature -->

That is the version of the core that sealed this record. The answer is `verified` (backed by a valid RFC 3161 stamp), `pending` or `failed` (something does not match), with times in UTC and in Central Mexico time. A record with OpenTimestamps only stays `pending` even when its proof is complete: this version does not check the Bitcoin block, so do it by hand with the commands of point 3. With a network, it queries the OpenTimestamps calendars to complete a pending proof in memory; `--upgrade` also saves it in `{{sealed}}.ots`, and `--offline` does not use the network.

## 2. The RFC 3161 stamps, with OpenSSL

<!-- tsa -->
This record carries these stamps:

| TSA | Verifiable date (UTC) | `-attime` | `sha256sum` of the root | Root fingerprint (`openssl x509`) | Where the TSA publishes its root |
|---|---|---|---|---|---|
{{tsa_table}}

For each TSA, with OpenSSL 1.1 or later (Git for Windows ships it):

1. Check that the root is the one of the TSA. The first two commands compute the two values of the table; compare one of them with the fingerprint that the TSA publishes on its page (the last column). On macOS, `shasum -a 256` does the same as `sha256sum`.
2. Check the stamp. The third command must answer `Verification: OK`. `-attime` is the verifiable date of the stamp in seconds since 1970: that way OpenSSL accepts a certificate that has expired today but was valid when sealing.

```sh
{{tsa_commands}}
```

`evidence verify` also checks the revocation status stored in `tsa/<id>.ocsp.der` or `tsa/<id>.crl.der`.
<!-- /tsa -->
<!-- no-tsa -->
This record carries no RFC 3161 stamps. The seal of a submission uses OpenTimestamps only; the seal of an integration requests two TSAs and OpenTimestamps. `record.json` says which proofs were requested and why any is missing.
<!-- /no-tsa -->

## 3. The OpenTimestamps proof

With the official client (`pip install opentimestamps-client`):

```sh
ots --no-bitcoin verify {{sealed}}.ots
ots info {{sealed}}.ots
```

When sealing, the proof stays pending until Bitcoin includes its block, which takes a few hours. `ots upgrade {{sealed}}.ots` completes it once the calendars have it. Once complete, `ots info` shows the block height and its Merkle root: compare it with the one of that block in a Bitcoin explorer or in your own node. That comparison is what gives the date: `evidence verify` shows the same height and root, but does not compare them, because anyone can write a fake Bitcoin attestation; that is why it leaves the record `pending`. If the folder has no `{{sealed}}.ots`, that proof was not obtained, and `record.json` says why.

<!-- signature -->
## 4. The project signature, with ssh-keygen

`manifest.json.sig` is the OpenSSH signature (SSHSIG) of `manifest.json`, with the namespace `{{namespace}}` and key `{{key_id}}`, whose fingerprint is `{{key}}`. The stamps of points 2 and 3 are of the signature: they date the signature and, through it, the manifest.

With the `allowed_signers` file that the group publishes and OpenSSH 9.1 or later (the `ssh-keygen` of Windows 11 and of Git for Windows are):

```sh
ssh-keygen -Y verify -f allowed_signers -I {{key_id}} -n {{namespace}} -s manifest.json.sig -O verify-time={{verify_time}} < manifest.json
```

It must answer `Good "{{namespace}}" signature for {{key_id}}`. `verify-time` is the verifiable date of the earliest RFC 3161 stamp: that way `ssh-keygen` checks that the key was valid when it signed, even if it has been rotated or revoked since.

Without that file, this command checks the signature and shows the fingerprint of the key:

```sh
ssh-keygen -Y check-novalidate -n {{namespace}} -s manifest.json.sig < manifest.json
```

Compare that fingerprint with the one the group publishes and check in its list of keys that the key was valid at the date of the stamp (`since` and `until`).

<!-- /signature -->
## {{open_section}}. Opening the commitments without the core

```sh
python open_commitments.py manifest.json openings.json
python open_commitments.py manifest.json openings.json <folder>
```

The first form checks that `openings.json` belongs to this manifest and that each opening reproduces its commitment. The second one also compares each file with the sealed content. `<folder>` is a copy of the files of commit `{{commit}}` with their git bytes, without converting line endings; for example, the one left by `git -c core.autocrlf=false clone` followed by `git checkout {{commit}}`. Each line says `ok`, `different` (the file is not the sealed one), `missing` (the file is missing) or `not-in-manifest` (the opening does not belong to this manifest). An absolute path or one with `..` gives `different` without being read, and anything in a path that cannot be printed is shown escaped. The program exits with 0 if everything matches.

The formulas, with `‖` as byte concatenation and SHA-256 in hexadecimal:

- path: `p = SHA-256("evidence.path.v1" ‖ 0x00 ‖ salt ‖ path)`, with the path in the exact bytes given by git;
- content: `c = SHA-256("evidence.blob.v1" ‖ 0x00 ‖ salt ‖ content)`, with the bytes of the file in git; `c` is null if the file was deleted;
- actor: `a = SHA-256("evidence.actor.v1" ‖ 0x00 ‖ salt ‖ role ‖ 0x00 ‖ value)`, with the role `pr_author`, `merged_by`, `author` or `committer`.

<!-- no-signature -->
The hash of `manifest.json` (`sha256sum manifest.json`) must be `{{manifest_sha256}}`: the same as in `record.json` and the one the proofs sealed.
<!-- /no-signature -->
<!-- signature -->
The hash of `manifest.json` (`sha256sum manifest.json`) must be `{{manifest_sha256}}`: the same as in `record.json`. The proofs sealed the signature `manifest.json.sig`, which only verifies with these bytes.
<!-- /signature -->
