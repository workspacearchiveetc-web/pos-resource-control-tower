# K3 Protected Repository Source Identity Snapshot — 2026-10-06

Purpose: preserve exact source identity for the two repositories that K3 Option B intends to exclude from GitHub App installation 166062286.

This record is intentionally published in the retained repository `pos-resource-control-tower` (repository ID 1404017584), outside the two protected repositories, so the source identity remains available after exclusion.

## autonomous-dispatch-v0

- Repository: workspacearchiveetc-web/autonomous-dispatch-v0
- Repository ID: 1390443786
- Default branch: main
- Exact main HEAD: `93362e9b69c05582f81b1295207a849bad94751e`
- Exact HEAD tree: `bf94652218ff20d51616901a8b541af4c63762e7`
- HEAD commit author date: 2026-09-29T05:10:38Z
- HEAD commit message begins: `releases: latest-package.zip = git archive 0f8f760`
- Connector readback at capture time showed admin/maintain/pull/push/triage access.

## autonomous-dispatch-v0-pilot

- Repository: workspacearchiveetc-web/autonomous-dispatch-v0-pilot
- Repository ID: 1394207795
- Default branch: main
- Exact main HEAD: `a63af4610b2b7fc9524fd0cfb30064b01634f8d5`
- Exact HEAD tree: `68c9f82ff4b254f8163a83faf0512f337433c4d8`
- HEAD commit author date: 2026-09-29T03:40:30Z
- HEAD commit message begins: `releases: latest-package.zip = git archive 0debc40`
- Connector readback at capture time showed admin/maintain/pull/push/triage access.

## Evidence status

Completed here:
- exact repository IDs
- exact default branches
- exact protected `main` HEAD commit SHAs
- exact corresponding tree SHAs
- publication into a retained repository outside the protected pair

Still required before K3 prerequisite #6 can be called COMPLETE:
- deterministic export/archive of each protected repository at the recorded HEAD
- SHA-256 (or equivalent approved digest) of each exported artifact
- independent readback/review proving each published export matches the recorded source identity and digest
- explicit durable handoff location for the exported bytes after exclusion

This artifact is evidence, not a claim that K3, R2, AD, or G1 is complete.
