# How to verify the signature of this evidence package

This folder, `{{folder}}`, is the **project signature** on the index of the package (`index.txt`), with its time stamps. Key `{{key_id}}` signed it, and its fingerprint is `{{key}}`. The index lists {{files}} files, and its SHA-256 hash is `{{index_sha256}}`. The package is all you need to verify it: no access to the repository is required.

## What it proves and what it does not

- **It proves** that whoever holds the private key of the project signed this index, and that the index and each file it lists already existed at the date of the stamps and have not changed since. The verifiable date comes from the RFC 3161 time stamps and from Bitcoin, through OpenTimestamps.
- **It does not prove** who wrote each change or who the owner of the content is. Authorship rests on the authorship agreement, on the CRediT statement and on the records of the forge.
- Compare the fingerprint of the key with the one the group publishes in its public repository or in its Zenodo record: only then do you know the key is the project's.

## What is in the folder

| File | What it is |
|---|---|
| `signature.json` | The index of the signature: the hash of `index.txt`, the key and the proofs that were requested. |
| `index.txt.sig` | The OpenSSH signature (SSHSIG) of `../index.txt`. It is what the proofs stamped. |
| `index.txt.sig.ots` | The OpenTimestamps proof, if it was obtained. |
| `tsa/<id>.tsr` | Each RFC 3161 stamp, exactly as the time-stamping authority (TSA) returned it. |
| `tsa/<id>.chain.pem` and `tsa/<id>.root.pem` | The certificate chain of the TSA and the root it was checked against when sealing. |
| `tsa/<id>.ocsp.der` or `tsa/<id>.crl.der` | The revocation status of the TSA certificate, captured when sealing. |

## 1. With a single command

With [uv](https://docs.astral.sh/uv/) installed, from the folder of the package (the one with `index.txt`):

```sh
uvx --from "{{name}}[evidence]=={{version}}" {{name}} evidence verify . --keys <list>
```

`<list>` is the list of keys that the group publishes (a YAML file with the key `keys`). The answer verifies each record of the package and this signature, and gives the origin of each record: `project` if a project signature that was valid at the date of the stamp covers it.

## 2. The files of the package, with sha256sum

From the folder of the package:

```sh
sha256sum -c index.txt
```

Each line must end in `OK`. On macOS, `shasum -a 256 -c index.txt` does the same. A `FAILED` line is a file that changed after the index was built.

## 3. The signature, with ssh-keygen

From the folder of the package, with the `allowed_signers` file that the group publishes and OpenSSH 9.1 or later (the `ssh-keygen` of Windows 11 and of Git for Windows are):

```sh
ssh-keygen -Y verify -f allowed_signers -I {{key_id}} -n {{namespace}} -s {{folder}}/index.txt.sig -O verify-time={{verify_time}} < index.txt
```

It must answer `Good "{{namespace}}" signature for {{key_id}}`. `verify-time` is the verifiable date of the earliest RFC 3161 stamp: that way `ssh-keygen` checks that the key was valid when it signed, even if it has been rotated or revoked since. Without that file, this command checks the signature and shows the fingerprint of the key, which you compare by hand with the published one:

```sh
ssh-keygen -Y check-novalidate -n {{namespace}} -s {{folder}}/index.txt.sig < index.txt
```

## 4. The RFC 3161 stamps, with OpenSSL

<!-- tsa -->
This signature carries these stamps:

| TSA | Verifiable date (UTC) | `-attime` | `sha256sum` of the root | Root fingerprint (`openssl x509`) | Where the TSA publishes its root |
|---|---|---|---|---|---|
{{tsa_table}}

From this folder, with OpenSSL 1.1 or later: compare one of the two root fingerprints with the one the TSA publishes, and the last command of each TSA must answer `Verification: OK`.

```sh
{{tsa_commands}}
```
<!-- /tsa -->
<!-- no-tsa -->
This signature carries no RFC 3161 stamps: no TSA answered when signing. `signature.json` says which proofs were requested. Without an RFC 3161 stamp, `evidence verify` leaves the signature `pending`.
<!-- /no-tsa -->

## 5. The OpenTimestamps proof

From this folder, with the official client (`pip install opentimestamps-client`):

```sh
ots --no-bitcoin verify index.txt.sig.ots
ots info index.txt.sig.ots
```

The proof stays pending until Bitcoin includes its block, which takes a few hours; `ots upgrade index.txt.sig.ots` completes it. Once complete, compare the height and the Merkle root that `ots info` shows with the ones of that block in a Bitcoin explorer or in your own node.
