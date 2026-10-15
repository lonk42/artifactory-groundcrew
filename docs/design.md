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
  - [Y7. Whole trees from upstream's file list](#y7-whole-trees-from-upstreams-file-list)
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
Requirements 2, 3 and 7 apply to tier 1 only since 2026-10-08: tier 2 only adds ([below](#tier-2-additive-and-demand-driven)).

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

### Y7. Whole trees from upstream's file list

Added 2026-10-15.
Y6 lists a yum repository from its `repomd.xml`, so it sees repository roots and nothing beside them:
not the keys, `media.repo` or images next to `repodata/`, nor the release directory around them.
On Rocky, `9` is a symlink to the current minor release, `9.8`.
Mirroring `9/` whole, and following it to the next minor release unattended, needs a list of the whole tree.

Rocky and Fedora publish one for `quick-fedora-mirror`.
`fullfiletimelist-<name>`, at the root, names every file, directory and symlink with its modification time and size.
Read through the remote like any metadata, it replaces `rsync --list-only` in Y6 step 1; the rest of Y6 is unchanged.

- **Selection by path.** Include and exclude patterns, as globs or `re:` regexes.
  This is the only tier 1 type that selects within upstream, and only by path.
  One Rocky release is about 2.2 TB, most of it ISOs, images, debug and source packages.
- **Symlinks.** The list gives a link's target length, not its target.
  A link to a file is fetched at its own path, and upstream's web server follows it.
  A link to a directory is copied under its own name, from the sibling directory whose name is as long as the target.
  Candidates of equal length are told apart by reading, through the link, a file only one of them has.
  `9/` and `9.8/` are then both held, stored once.
- **Changes.** The list has no checksums.
  Each file carries upstream's mtime as the `groundcrew.mtime` property, and a different mtime fetches it again, after expiring the remote's cached copy.
  Retargeting a symlink recreates it, so its mtime moves too.
  `repomd.xml`, its key and its signature are read and compared by checksum, and the files `repomd.xml` lists by the checksums it gives.
- **Consistency.** Each `repomd.xml` must list only files the list has, or the cycle stops as upstream mid-update.
  Files under `repodata/` are published after every other file, with each `repomd.xml` and its signature last.
- **Polling.** The list is the probe; an unchanged list skips the cycle.
- Cost: Rocky's list is 75 MB and 728,000 lines, read every cycle.
  Parsing it took about 360 MiB of memory, and a whole cycle of Rocky 9 and 10, 675,000 files, peaked at 483 MiB.
- Not handled: a directory link inside another link's target is mirrored only at its own path.
  A link with no sibling of its target's length, or with both a file and a directory of that length, is skipped and logged as `tree.link_skipped`.

**Built** 2026-10-15, for upstreams that publish such a list.

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

D1 was recommended first: a Docker local repository is an ordinary registry, so these tools push straight into it, and manifests and blobs keep their upstream digests.
D2 is built instead, on 2026-10-08, once its layout was verified ([verification](verification.md#2026-10-08-groundcrew-030-docker)).

- D1 pulls from the upstream registry, so the pod needs internet access ([goal 9](goals.md#goals)), and every blob passes through the pod on its way back to Artifactory.
- D2 reads manifests through the remote's registry API and has the remote fetch each blob with a `HEAD`, so no blob passes through the pod.
- D2's remote cache is the demand signal for [additive tier 2](#tier-2-additive-and-demand-driven), as for PyPI and npm.
- The remote caches an index at `<image>/<tag>/list.manifest.json` and each platform at `<image>/sha256__<digest>/`; a single-platform tag keeps its manifest and blobs in `<image>/<tag>/`.
  A copy of those paths into a Docker local repository serves the tag with upstream's digest.

The list sets image, tag and platforms; [tag rules](#tag-rules) select tags by pattern.

Docker Hub allows 100 pulls per 6 hours unauthenticated ([Docker][docker-limits]), so the sync needs an account.

#### Tag rules

Built 2026-10-12 ([verification](verification.md#2026-10-12-groundcrew-040-docker-tag-rules)).
A list entry names one tag, and a moving tag such as `latest` keeps only its current target under its name.
Following an image's releases needs a rule that selects tags from those upstream publishes.

Tags have no shared scheme: `1.37.0`, `16.4-alpine3.20`, `v2.0`, `2024-05-01`, `latest`.

| Option | Any tag scheme | Backfills | Cost |
|---|:---:|:---:|---|
| **T1.** Semver range, as `image>=0.17.0` | ❌ | ✅ | One tag list |
| **T2.** Regex on the tag, in natural-sort order | ✅ | ✅ | One tag list |
| **T3.** Tags first seen after the rule was added | ✅ | ❌ | One tag list, and state |
| **T4.** Order by image creation date | ✅ | ✅ | One config blob per candidate tag |

T2 is built.

- T1 rejects suffix variants and dates.
  Loose semver, ignoring a `v` prefix and a suffix, still cannot order `alpine3.20` after `alpine3.19`.
- T3 needs Groundcrew's own state, which [provenance](#provenance) avoids.
  A lost state volume makes every tag new, or none.
- T4 reads a blob for every tag the pattern admits, thousands for `library/node`.

```yaml
docker:
  follow:
    - image: library/postgres
      match: '\d+\.\d+-alpine'
      from: '16.0-alpine'
      newest: 5
```

| Field | Effect |
|---|---|
| `image` | The image, with its namespace |
| `match` | A regex the whole tag must match |
| `from` | Keep tags at or after this one in natural-sort order. It need not exist upstream |
| `newest` | Keep the last N in natural-sort order, after `from` |

At least one of `from` and `newest` is required, so a broad pattern cannot select an image's whole history.
`platforms` applies to every selected tag, as to listed ones.

Natural-sort order splits a tag into runs of digits and non-digits.
Digit runs compare as numbers, the rest as text, and a digit run sorts before a text run at the same position.
`1.10.0` follows `1.9.2`, and `16.4-alpine3.20` follows `16.4-alpine3.19`.
A pre-release sorts after its release (`1.0.0-rc1` after `1.0.0`); `match` excludes pre-releases where that matters.

Each rule lists the image's tags with `GET /v2/<image>/tags/list` through the remote, following pagination, then filters by `match`, `from` and `newest` in that order.
The selected tags resolve as list entries do, with `groundcrew.source` `config` and `groundcrew.requirement` the `image:tag`.

- **Detection.** `probe` returns a hash of each rule's selected tags, so a new tag starts a resolve within one interval, not at the next daily resolve.
  The tag list is one call per rule per cycle; a Docker Hub tag list is not a pull.
- **Removal.** Tier 2 deletes nothing, so a tag that leaves `newest` stays in the local repository.
  `newest` limits what is fetched, not what is held.
- **Moved tags.** Each daily resolve reads every selected tag's manifest again, as for listed tags: N manifest pulls per rule per day.
- **Demand.** A pulled tag that a rule also selects is resolved once, as a listed tag already is in `demand.refresh`.

Verified on Artifactory 7.173.4 before building:

- A Docker remote answers `tags/list` from upstream, for Docker Hub and ghcr, including images it has never cached, with `listRemoteFolderItems` off.
  Nothing is stored in the remote cache.
- The remote passes upstream's `Link` header through unchanged, naming the bare `/v2/` path, which Artifactory answers 404.
  Groundcrew re-bases each next page onto `/api/docker/<remote>/v2/`.
- Artifactory sorts the list; ghcr's own order is arbitrary. The rule sorts it anyway.
- Not yet shown: that a tag published upstream appears in the remote's list at once, and is not held for `retrievalCachePeriodSecs`.

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
Built as `pip install --dry-run --report` per requirement and target, which names the closure without installing it.
npm is built the same way with `npm install --package-lock-only` per spec, which reads only package metadata; `os` and `cpu` filters stand in for PyPI's targets.
Groundcrew copies those files with the Y6 core, and Artifactory maintains the local index on every copy and delete, so no reindex call is needed.
P4 is the step up if version rules ("newest 3 releases") or upstream deletions (yanked releases, unpublished versions) need handling, which P3 does not do.

### Generic

A Generic remote per upstream host, with explicit paths in the list, fetched and copied with the Y6 core.
No other option adds anything here.

### Tier 2: additive and demand-driven

Agreed and built 2026-10-08, after PyPI.
Two changes to tier 2: clients' pulls through the remote add to the mirror, and tier 2 deletes nothing.
The reasons are in [background](background.md#tier-2-only-adds).

#### The signal

The remote's `-cache` repository.
A client using the virtual repository is served from the local repository first, so the cache only receives what the mirror lacks.
Each cycle lists the cache, and a package file not in the local repository is new demand.
Index pages and package metadata (`.pypi/`, `.npm/`, `.jfrog/`) are not demand, and neither is anything Groundcrew's own user cached.

Groundcrew's own resolution also downloads through the remote, but only what is already wanted, so it never appears as new demand.
Demand needs the remote, so it exists on the source instance only.

#### What a pull means

A cached file is one client's run: one Python version, one platform, and only what that client did not already have.
A client's own pip cache, `--no-deps`, a failed install, or a resolver rejecting a version it downloaded all leave the cached set different from an installable closure.

| Option | Requester served from local | Targets complete | Cost |
|---|:---:|:---:|---|
| **D-a.** Copy the pulled files | ✅ | ❌ | One copy per file |
| **D-b.** Resolve `name==version` for the configured targets | ❌\* | ✅ | pip per package per target |
| **D-c.** Both | ✅ | ✅ | Both |

- \* Unless the requester's platform is a target.

D-c is chosen.
On the source, a gap left by D-a is repaired by later demand, since the virtual falls back to the remote.
A client of the local repository alone, such as an air-gapped destination, has nothing to fall back to, and needs D-b's complete closures.
The name and version come from the file name; a pulled sdist is copied, and its targets resolve to wheels.
npm demand resolves `name@version` with the mirror's `os` and `cpu` filters.
Docker demand resolves the pulled `image:tag` for the configured platforms.
Tags move, so Docker tags brought in by demand are resolved again whenever the list is, found from their `groundcrew.requirement` properties.
Generic demand is D-a, since there is nothing to resolve.

#### Provenance

Which files came from the list and which from demand, recorded without state of Groundcrew's own.

| Option | Survives a cache cleanup | Stateless | Cost per cycle |
|---|:---:|:---:|---|
| The cache is the list | ❌ | ✅ | None |
| Groundcrew's state volume | ✅ | ❌ | None |
| A pull request to the config repository | ✅ | ✅ | Git credentials and a reviewer |
| **Artifactory properties on each file** | ✅ | ✅ | One call per newly copied file |

Properties are chosen, set once when a file is copied in:

| Property | Value |
|---|---|
| `groundcrew.source` | `config` or `demand` |
| `groundcrew.requirement` | The list entry, or the `name==version` inferred from a pull |
| `groundcrew.pulled_by` | The cache file's `created_by`; demand only |
| `groundcrew.ingested` | When it was copied |

They record why a file first arrived; a file later wanted for a second reason is not re-tagged.
One AQL query on `@groundcrew.source` lists either kind.
Replication carries properties to destination instances.
Verified on Artifactory 7.172.6: a PyPI local accepts multi-valued properties, AQL finds files by them, and the package index is unaffected.

#### Cost

| Step | Calls | Scales with |
|---|---|---|
| List the local and the cache | Two AQL queries | Files held |
| Ingest demand | Copy, pip per target, copy the closure, tag | New pulls |
| Resolve the list | pip per entry per target | The list; daily |

Once copied, a pulled file is in the local repository and drops out of the comparison, so nothing is processed twice.
The list is re-resolved daily, using the same `last_full` rule as tier 1, since only unpinned entries change and nothing is deleted.
Demand is ingested every cycle.

#### Not solved

- **Removal.** Deferred; see [background](background.md#open-questions).
- **Retries.** A pulled package that does not resolve for a target is copied anyway, and is not new demand again.
  A transient pip failure leaves that target without it.
- **New targets.** A target added to the list applies to later pulls; earlier pulls are not resolved for it.
- **Groundcrew's own identity.** Read from the token's subject (`/access/api/v1/tokens/me`), or the username for basic auth.
  If it cannot be read, Groundcrew's own downloads count as demand.
- **Growth.** Every release a client installs through the virtual is demand, and unpinned list entries keep each release they resolved to.

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
As built, docker uses D2; see [Docker](#docker).

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

- PyPI and npm dependency resolution: the real clients, through the remote.

### Shape of the tool

As built in 0.3.0; [goals.md](goals.md) records the goals it was built to.

- One Deployment running a daemon loop, with per-mirror intervals from a config file in git.
  Not a sidecar: it needs only the REST API, so it runs in Artifactory's namespace or elsewhere.
- A shared core: compare, fetch through the remote, copy to local, metadata last, prune.
- Enumerators for yum, apt (suite archives and flat repositories), explicit file lists, PyPI and npm (resolved by pip and npm through the remote), and Docker tags (read through the remote's registry API).
- Each tier 1 mirror owns a `scope` in its local repository. Unlisted files under it are deleted, which is how removing a list entry removes its content.
- Tier 2 only adds: its list, resolved daily, and what clients pull through the remote, each file tagged with why ([Tier 2](#tier-2-additive-and-demand-driven)).
- Repository creation is optional. Generic repositories use the `simple-default` layout; the REST API otherwise defaults to `maven-2-default`.

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
