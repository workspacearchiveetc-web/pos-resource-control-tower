# K3 Protected Repository Export Manifest — 2026-10-07 (UTC)

Purpose: K3 prerequisite #6. Digest manifest for the exact-head export of the two repositories that K3 Option B
excludes from GitHub App installation 166062286. Follows `K3-PROTECTED-SOURCE-IDENTITY-20261006.md` (same heads, same trees).

This file is published in the retained repository `pos-resource-control-tower` (1404017584), which is public, so it carries
digests only: no source bytes and no file paths. The bytes are in a private prerelease of the retained repository
`pos-code` (1386800024), outside the protected pair:
https://github.com/workspacearchiveetc-web/pos-code/releases/tag/k3-protected-export-20261007

## Source identities (unchanged from the 2026-10-06 snapshot; live `main` re-read at export time)

| Repository | ID | Ref | HEAD | Tree | Commits reachable | Files |
|---|---|---|---|---|---|---|
| workspacearchiveetc-web/autonomous-dispatch-v0 | 1390443786 | refs/heads/main | `93362e9b69c05582f81b1295207a849bad94751e` | `bf94652218ff20d51616901a8b541af4c63762e7` | 131 | 4362 |
| workspacearchiveetc-web/autonomous-dispatch-v0-pilot | 1394207795 | refs/heads/main | `a63af4610b2b7fc9524fd0cfb30064b01634f8d5` | `68c9f82ff4b254f8163a83faf0512f337433c4d8` | 109 | 3236 |

No submodules (every tree entry is a blob). The tar archives are byte-complete: v0's in-tree `releases/ export-ignore`
attribute was neutralized in the local export clone only, and every tar member was re-hashed against the per-file manifest.

## SHA256SUMS (verbatim; the release also carries this file)

```
635b3ec952e734bfa46117fa117babadbfeee87f20b35a62a55a0737d507f1e8  EXPORT-MANIFEST.json
44c74b973482aa42b911430640ee76a397998df7251a0591527702558761a6f6  autonomous-dispatch-v0-93362e9b69c0.bundle
4f9fc8e7f7849aad29a7bae44000b2adbe416983d501517e7eba511de60e701e  autonomous-dispatch-v0-93362e9b69c0.files.tsv
f3089ae70996b0fb96ba4add83266ce4c47b5f77d98f4e99e4e439da459e3cba  autonomous-dispatch-v0-93362e9b69c0.tar
27b99c884bd9d606bc464e1356d470f1086413ca731a4b9cf8f6d82133211f74  autonomous-dispatch-v0-pilot-a63af4610b2b.bundle
36e00d95a40ed723f250ca71c6ee4507211b720a66b493c88ea78b619e3138a1  autonomous-dispatch-v0-pilot-a63af4610b2b.files.tsv
285fe934a435d2d29000fdecd7ad3230e102b3c1b63b8b0332d89a288087c399  autonomous-dispatch-v0-pilot-a63af4610b2b.tar
```

SHA256SUMS itself: `45f5a652b67e2c951d45166e23ac7af00808da23770806282a39cba95af2cf45`.

## How to check (needs no access to either protected repository)

1. Download the release assets; `shasum -a 256 -c SHA256SUMS`.
2. `git bundle verify <bundle>`; `git clone --branch main <bundle> x`; `git -C x rev-parse HEAD HEAD^{tree}` must print the HEAD and Tree above.
3. Hash every member of the tar and compare with the sha256 column of `<name>.files.tsv`.

## Provenance and custody

- Producer: `tools/k3_protected_export.py` on autonomous-dispatch-v0 branch `recovery/k3-prereq5-6-machine-completion-20261006`
  (Claude Code builder session, Mac Studio, GitHub identity workspacearchiveetc-web via gh CLI OAuth token).
- Access to the protected pair: `git clone --bare --single-branch` (read-only). No push, ref or settings change on either.
- Export created 2026-10-07T02:23:56Z; release published ~02:35Z; release readback (download + steps 1–3) PASS ~02:36Z.
  The release page's `createdAt` shows the tagged pos-code commit's date, not the publish time.
- Custody copies: GitHub release above (private, retained repository); Brian's BLUE SSD staging folder
  `POS-staged/k3-protected-export-20261007T021554Z-r2/`.

Evidence only. This is not a claim that K3, R2, AD or G1 is complete.
