# Project data

Raw data is never versioned.
This table only describes each dataset and says where it is and how to request access.
Each person downloads dataset `<id>` into `data/<id>/`, which git ignores.

| id | description | location | access | classification | version |
|---|---|---|---|---|---|

How to fill in each row (the table headers are not translated):

- `id`: unique identifier in lowercase with hyphens, for example `school-survey`.
- `description`: what the dataset contains, in one line.
- `location`: URL or location of the original, without credentials or signed links.
- `access`: how to obtain it, for example "public" or "ask the lead for access".
- `classification`: `public`, `internal`, `restricted` or `local_only`.
- `version`: version or wave, and preferably its checksum (`sha256:…`).
