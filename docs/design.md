# Design: options analysis

Recommendation: write a small sync tool that tells Artifactory which files to fetch for full yum and apt mirrors,
and use existing tools for the list-driven ecosystems.
No existing tool mirrors a yum or apt repository into Artifactory as a single copy,
and the one product designed for this (Pulp) would store content separately from Artifactory instead of filling it.

Researched 2026-09-23.

## Contents

- [Requirements](#requirements)
- [Where mirrored content lives](#where-mirrored-content-lives)
- [Full mirrors: yum and apt](#full-mirrors-yum-and-apt)
- [Curated lists: docker, PyPI, npm, generic](#curated-lists-docker-pypi-npm-generic)
- [Comparison](#comparison)
- [Recommendation](#recommendation)
- [Sources](#sources)

## Requirements

Artifactory is self-hosted, with internet access.

Two shapes of content are in scope.

- **Full mirrors (tier 1).** yum and apt repositories, mirrored whole and kept current with upstream.
- **Curated lists (tier 2).** docker, PyPI, npm and generic, where mirroring everything is not feasible, so a maintained list defines what is kept.

Requirements:

1. **Served as-is.** Clients see repositories as the internet has them: upstream metadata, upstream signatures, upstream keys.
   Nothing is re-keyed or re-indexed unless the format forces it.
2. **Persistent.** Content stays on the source until upstream removes it.
   It does not age out for lack of requests.
3. **Deletions follow upstream.** When upstream removes a file, the mirror removes it too.
4. **One copy.** No second, mirror-sized staging copy beside Artifactory.
5. **404 on unmirrored content is expected.**
6. **Half-synced states are acceptable.** A repository mid-sync is incomplete until the next cycle.
7. **Deletions also follow the configuration.** Removing a path or list entry deletes its content.
8. **Talks only to Artifactory.** Upstream is reached through remote repositories, so the sync needs no internet access of its own.
9. **Repository creation is optional.**

Requirements 7 to 9 were agreed after this analysis was written, with the design goals in [goals.md](goals.md).
None of them changes the ranking below.

## Where mirrored content lives

Mirrored content lives in local repositories.
Remote repositories are used only as the fetch mechanism.

| | Remote `-cache` repository | Local repository |
|---|---|---|
| Persistence | Kept unless a cleanup period or policy is set. One admin setting can change that | Nothing in Artifactory removes it automatically |
| Upstream deletions | Never applied. Grows forever | Applied by the sync job |
| Metadata served | Upstream's. PyPI and npm links are rewritten when served, not stored ([verified](verification.md#results)) | Upstream's for Generic. Artifactory-built for typed npm and PyPI |

JFrog supports copying from a `-cache` repository into a local one,
and Artifactory stores blobs by checksum, so the copy costs no extra storage ([JFrog KB][kb-copy]).
JFrog's own guidance uses the same layout: local, remote and virtual repositories ([JFrog blog][blog-airgap]).

A virtual repository fronts the local and remote repositories, so clients have one URL.

## Full mirrors: yum and apt

Six options were considered.
Only two meet every requirement, and only one of those avoids an unproven dependency.

Public mirrors sync with rsync in two stages: packages first with nothing deleted, then the indices, then deletions.
Debian's `ftpsync`, Ubuntu's archive mirror guidance and Fedora's `quick-fedora-mirror` all follow this ordering,
which keeps a mirror from publishing an index that points at packages it does not yet hold.
Every option here keeps that ordering.

### Y1. Artifactory pull replication

Pull replication fills a remote repository's cache on a schedule.
It only works when the source is another Artifactory:
"Pull replication can't be used to replicate a remote resource that is not an Artifactory repository" ([JFrog docs][jfrog-replication]).

**Ruled out.**

### Y2. Warm the remote repository and serve its `-cache`

A script requests every file through a remote repository, and clients read the `-cache`.

- Meets: as-is, one copy.
- Fails: persistence depends on cleanup settings, and upstream deletions are never applied.

**Rejected** on persistence and deletions.

### Y3. Standard mirror tools into a staging volume, then upload

rsync, `dnf reposync --download-metadata --delete` or `debmirror` write a full mirror to a persistent volume.
`jf rt upload` then pushes packages, then metadata, then runs `--sync-deletes` against a Generic local repository.
This is JFrog's documented air-gap route: download the resources, then upload them to a local Generic repository ([JFrog docs][jfrog-airgap]).

- Meets: as-is, persistent, deletions, two-stage ordering.
  All tooling is standard and widely deployed.
- Fails: one copy.
  The staging volume is mirror-sized and has to persist, since rsync and reposync need the previous tree to compute a delta.
- Cost: storage for the second copy, and every byte written twice.

**Rejected** on the one-copy requirement.
It is the fallback if that requirement is relaxed.

### Y4. rclone streaming from upstream into Artifactory WebDAV

rclone reads the upstream mirror over HTTP and writes to a Generic local repository through Artifactory's WebDAV endpoint, with no local copy.
Two runs give the two-stage ordering: `rclone copy` excluding metadata, then `rclone sync` with `--max-delete` as a brake.

- Meets: as-is, persistent, deletions, one copy.
  No code.
- Risk: WebDAV does not keep upstream modification times, so comparison is by size only.
  Fixed-name metadata such as `repomd.xml` has to be re-sent every run.
- Risk: a restic user reported wrong listings from Artifactory WebDAV through rclone, with no root cause found ([restic #3936][restic-3936]).
- Risk: WebDAV PROPFIND behaviour on directories of tens of thousands of files, such as Debian's `pool/main`, is unknown.
- Needs HTML directory listings upstream, or a file list fed in with `--files-from`.

**Viable, but unproven.**
Every risk is in a component we do not control.

### Y5. Pulp

[Pulp](https://pulpproject.org/) is the engine behind Red Hat Satellite and Foreman/Katello,
and the closest finished product for this problem.

- rpm `sync_policy: mirror_complete` publishes a bit-for-bit copy, original metadata and signatures included.
  It refuses repositories that use `xml:base`, delta RPMs, or metadata with duplicate package IDs ([pulp_rpm][pulp-rpm], [#2398][pulp-rpm-2398]).
- deb verbatim publication republishes upstream `Release` and `InRelease` byte for byte ([pulp_deb][pulp-deb]).
  By-hash support is open ([#1084][pulp-deb-1084]),
  and a Foreman community thread concludes full Ubuntu mirroring is not production-ready through Katello ([thread][foreman-thread]).
- It has its own export and import for air gaps.

Pulp keeps content in its own storage.
Used to fill Artifactory, it needs a second copy again.
Avoiding that means Pulp replaces Artifactory for OS repositories.
The result is two repository managers.

**Rejected** on one copy, or on architecture if used standalone.

### Y6. Enumerate, then Artifactory fetches and copies

A remote repository points at the upstream mirror, and a Generic local repository holds the mirror.
A sync job builds the upstream file list and leaves the downloading to Artifactory:

1. **List upstream.** `rsync --list-only` against the mirror, or parse `repomd.xml` and `primary.xml`, or `InRelease` and `Packages`.
   `dnf reposync --urls` prints the yum list without downloading.
2. **Compare** against the local repository with one AQL query.
3. **Fetch** each new package through the remote with Artifact Sync Download (`GET /api/download/<remote>/<path>?content=none`),
   which caches the file without returning the body.
4. **Copy** from the `-cache` into the Generic local with `/api/copy`.
   No storage cost, since blobs are stored by checksum.
   Metadata is expired from the `-cache` before it is read, because a Generic remote never re-fetches a cached file ([measured](goals.md#reading-metadata-through-a-remote)).
5. **Metadata last.** Repeat steps 3 and 4 for the index files once every package is in place.
6. **Prune** files no longer listed upstream.

- Meets: as-is, persistent, deletions, one copy, two-stage ordering.
- Uses only documented Artifactory APIs. No WebDAV, no staging.
- Upstream traffic goes through Artifactory's own proxy and credential settings.
- Cost: code we own. Groundcrew 0.1.0 is about 1,600 lines including its CLI, with parsers for yum and apt metadata.
- Cost: one or two API calls per new file.
  The first sync of a large distribution is tens of thousands of calls.
  Later syncs touch only the delta.

**Recommended**, and since built and verified on a live instance: see [verification.md](verification.md).

## Curated lists: docker, PyPI, npm, generic

For list-driven content, existing tools cover docker, and the Y6 core covers the rest with a different enumerator.
Full ecosystem mirrors are out of scope, since the storage is not feasible, so every option here starts from a list kept in git.

### Docker

| Option | As-is | One copy | Upstream deletions | Code |
|---|:---:|:---:|:---:|---|
| **D1.** `regsync` or `skopeo sync` pushing into a Docker local repository | ✅ | ✅ | ❌\* | None |
| **D2.** Pull through a Docker remote, copy `-cache` to local | ✅ | ✅ | ✅ | Enumerator |
| **D3.** Pulp container plugin | ✅ | ❌ | ✅ | None |

- \* Neither tool removes a tag that upstream deleted.
  Needs a small prune step, or acceptance that deleted tags remain.

D1 is recommended.
A Docker local repository is an ordinary registry, so these tools push straight into it, and manifests and blobs keep their upstream digests.
The list sets image, tag rule (exact, regex, newest N) and platforms.
D2 carries a layout risk: Docker Hub official images sit under `library/` in a remote cache, and the tag-folder layout after a copy is unverified.

Docker Hub allows 100 pulls per 6 hours unauthenticated ([Docker][docker-limits]), so the sync needs an account.

### PyPI and npm

Upstream index pages cannot be served as-is to clients without internet access.
PyPI simple pages link to `files.pythonhosted.org`, and npm package documents link to `registry.npmjs.org`, with absolute URLs.
The index therefore has to be built by Artifactory from what the local repository holds.
Package files stay byte-identical, so pip `--hash` and npm `integrity` checks still pass.
The index lists only mirrored versions.

| Option | One copy | Handles dependencies | Notes |
|---|:---:|:---:|---|
| **P1.** bandersnatch (PyPI) | ❌ | ❌ | Mirrors every file of each listed project to disk. Filters by platform and newest N releases |
| **P2.** Pulp python and npm plugins | ❌ | ❌ | Same architecture cost as Y5. npm plugin maturity unverified |
| **P3.** Real clients through the remote, copy to local | ✅ | ✅ | JFrog's "dependency declaration" pattern ([JFrog blog][blog-airgap]) |
| **P4.** Enumerator reads package metadata, fetches chosen versions, copies to local | ✅ | Optional | Shares the Y6 core |

P3 is recommended to start.
The list is a requirements file or a `package.json` per set.
A job runs `pip download` or `npm install` through the remote, copies what landed from the `-cache` into the local repository, then calls the reindex API.
PyPI needs one run per target Python version and platform:
a wheel list resolved on Python 3.12 x86_64 does not serve a 3.11 or aarch64 client.
P4 is the step up if version rules ("newest 3 releases") or upstream deletions (yanked releases, unpublished versions) need handling, which P3 does not do.

### Generic

A Generic remote per upstream host, with explicit paths in the list, fetched and copied with the Y6 core.
No other option adds anything here.

## Comparison

| Option | As-is | Persistent | Upstream deletions | One copy | Documented behaviour only | Code to own |
|---|:---:|:---:|:---:|:---:|:---:|---|
| Y1 Pull replication | ❌ | ❌ | ❌ | ❌ | ❌ | Not possible |
| Y2 Serve `-cache` | ✅ | ❌ | ❌ | ✅ | ❌ | Enumerator |
| Y3 Mirror tools + staging upload | ✅ | ✅ | ✅ | ❌ | ✅ | Upload glue |
| Y4 rclone to WebDAV | ✅ | ✅ | ✅ | ✅ | ❌ | None |
| Y5 Pulp | ✅ | ✅ | ✅ | ❌ | ✅\* | None, plus a second platform |
| **Y6 Enumerate, fetch, copy** | ✅ | ✅ | ✅ | ✅ | ✅ | About 1,600 lines, built as Groundcrew 0.1.0 |

- \* Proven for rpm. Deb verbatim mirroring has open issues.

## Recommendation

Build Y6 as a small sync tool, use D1 for docker, and start PyPI and npm on P3.

### Why write code

1. **It is the only option that meets every requirement.**
   Each existing alternative breaks a stated one:
   Y3 and Y5 need a second copy, Y2 loses persistence and deletions, Y1 does not work against non-Artifactory sources.
2. **Nothing existing does this job.**
   The mirror tools (rsync, reposync, debmirror, Pulp) all write to their own filesystem or store.
   JFrog's own guidance for third-party content is "dedicated scripts" that loop over what is needed ([JFrog blog][blog-airgap]).
3. **The code is small, because Artifactory does the downloading and storage.**
   Downloading, storage, checksum de-duplication and serving stay in Artifactory.
   The tool lists, compares, orders and prunes.
4. **The core is needed anyway.**
   Generic lists, PyPI and npm version rules (P4) and upstream deletions all need the same fetch, copy and prune steps.
   Adopting Y4 for yum and apt would still leave this tool to write, alongside a second mechanism with a different failure model.
5. **It uses documented APIs only.**
   Sync Download, copy, AQL and delete.
   Y4, the only option that meets the requirements and requires no code, depends on WebDAV behaviour at scale that has a reported failure and no known root cause.
6. **Upstream traffic stays in Artifactory.**
   Proxy, credentials and egress rules are configured once on the remote repositories, not again in a sync tool.

### What is not written

- Docker sync: `regsync`, from a list in git.
- PyPI and npm dependency resolution: the real clients, through the remote.

### Shape of the tool

As built in 0.1.0; [goals.md](goals.md) records the goals it was built to.

- One Deployment running a daemon loop, with per-mirror intervals from a config file in git.
  Not a sidecar: it needs only the REST API, so it runs in Artifactory's namespace or elsewhere.
- A shared core: compare, fetch through the remote, copy to local, metadata last, prune.
- Enumerators for yum, apt (suite archives and flat repositories) and explicit file lists.
- Each mirror owns a `scope` in its local repository. Unlisted files under it are deleted, which is how removing a list entry removes its content.
- Repository creation is optional.
  A deployment with `role: destination` creates the repositories on an instance filled by other means.

### When to revisit

- If a second copy becomes acceptable, Y3 replaces the yum and apt enumerators with standard tools.
- If the Y4 WebDAV questions are resolved, it becomes an alternative for yum and apt that requires no code.
  The core is still needed for everything else.

## Sources

- [Repository Replication][jfrog-replication], JFrog
- [Working in Air-Gapped Environments][jfrog-airgap], JFrog
- [Using JFrog Artifactory With an Air Gap][blog-airgap], JFrog
- [Can't copy artifacts from remote repo to local repo][kb-copy], JFrog
- [Create, Sync and Publish a Repository][pulp-rpm], pulp_rpm
- [mirror_complete and duplicate packages, #2398][pulp-rpm-2398], pulp_rpm
- [Feature Overview][pulp-deb], pulp_deb
- [By-hash for verbatim publications, #1084][pulp-deb-1084], pulp_deb
- [Debian/Ubuntu full mirror with Pulp verbatim publish][foreman-thread], Foreman community
- [Restore via rclone WebDAV on Artifactory, #3936][restic-3936], restic
- [Revisiting Docker Hub Policies][docker-limits], Docker

[jfrog-replication]: https://docs.jfrog.com/artifactory/docs/repository-replication
[jfrog-airgap]: https://docs.jfrog.com/security/docs/working-in-air-gapped-environments
[blog-airgap]: https://jfrog.com/blog/using-artifactory-with-an-air-gap/
[kb-copy]: https://jfrog.com/help/r/artifactory-can-t-copy-artifacts-from-remote-repo-to-local-repo/artifactory-can-t-copy-artifacts-from-remote-repo-to-local-repo
[pulp-rpm]: https://pulpproject.org/pulp_rpm/docs/user/tutorials/create_sync_publish/
[pulp-rpm-2398]: https://github.com/pulp/pulp_rpm/issues/2398
[pulp-deb]: https://pulpproject.org/pulp_deb/docs/user/
[pulp-deb-1084]: https://github.com/pulp/pulp_deb/issues/1084
[foreman-thread]: https://community.theforeman.org/t/debian-ubuntu-full-mirror-with-pulp-verbatim-publish/40038
[restic-3936]: https://github.com/restic/restic/issues/3936
[docker-limits]: https://www.docker.com/blog/revisiting-docker-hub-policies-prioritizing-developer-experience/
