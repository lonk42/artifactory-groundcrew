# Background

How the requirements in [design.md](design.md) were arrived at.
Each requirement replaced an earlier assumption, and the rejected assumptions are the ones a new contributor is most likely to reintroduce.

## Starting point

The need was public package repositories kept current inside Artifactory, and served to clients as upstream publishes them.

The first framing was two groups:
yum and apt style repositories mirrored whole and auto-synced,
and the larger ecosystems (docker, pip, npm) driven by a maintained package list.
That grouping is unchanged.

## Requirements, and what each one ruled out

### Served as-is

Clients expect repositories as the internet has them, and need no special configuration.

This ruled out the first proposal: typed local repositories with Artifactory-generated metadata.
Artifactory signs the metadata it generates with its own key, so every client would have to trust a new key.
yum and apt mirrors therefore live in **Generic** local repositories holding upstream files byte for byte.

PyPI and npm cannot meet this in full.
Their upstream index pages carry absolute links to `files.pythonhosted.org` and `registry.npmjs.org`, which clients without internet access cannot reach.
Artifactory builds those indexes from the mirrored files, and the package files themselves stay identical.
This departure has not yet been explicitly confirmed; see [Open questions](#open-questions).

### Half-synced states and 404s are acceptable

An early concern was a client reading a repository during a sync, and getting metadata that points at packages not yet present.
A half-synced repository is complete after the next cycle.

The same applies to partial mirrors.
A PyPI or npm index listing a version that was never mirrored gives a 404, and that is expected behaviour.
Mirroring all of docker.io, ghcr.io, PyPI or npm is not feasible, so the list defines what exists.

The two-stage ordering (packages, then metadata, then deletions) is still kept, since standard mirror tooling does it anyway.

### Persistent until upstream removes it

The second proposal served remote repository `-cache` repositories directly.
It was rejected here.
Cached content survives only while no cleanup period or policy is configured, which is a guarantee one admin setting can remove.
A cache also never applies upstream deletions.

Content now lives in local repositories, and remote repositories are only the fetch path.

### One copy

Standard distro mirroring is rsync, in two stages: Debian's `ftpsync`, Ubuntu's archive mirror guidance, Fedora's `quick-fedora-mirror`.
rsync, `reposync` and `debmirror` all write to a filesystem and need the previous tree on disk to compute a delta.
Feeding Artifactory from them means a persistent, mirror-sized staging volume beside it.
JFrog's own air-gap documentation describes that route.

A second full copy was ruled out, which is what leaves Y4 (rclone into WebDAV) and Y6 (Artifactory fetches, the tool enumerates) as the only options that remain.

### Deletions follow the configuration too

Removing a path or list entry deletes its content, rather than freezing it.
The first build only owned the paths it listed, so removing one would have orphaned its files.
Each mirror now owns a `scope` in its local repository, and everything unlisted under it is deleted.
Two mirrors cannot claim overlapping scopes in one repository.

Groundcrew had a deletion brake, refusing any cycle that deleted more than 5% of a mirror, and it was removed on 2026-09-29.
Live testing showed ordinary updates to small tier 1 mirrors exceed it: one samba update replaces 26 of the 38 packages in Debian's `bookworm-updates`.
A cycle still deletes nothing when it fails to list upstream, fails a package, or finds an index that disagrees with its checksum.

### Repository creation is optional

Groundcrew creates the repositories its mirrors need when configured to, and not otherwise.
An instance that receives the repositories by other means needs them created too.
A deployment with `role: destination` creates only the local and virtual repositories there, and does not sync.

### Its own Deployment

Groundcrew needs only the REST API, so it runs as its own Deployment, and its failures do not affect Artifactory's pod.

### Talks only to Artifactory

Upstream metadata is read through the remote repository as well, so the pod needs no internet access.
This depends on expiring cached metadata first: a Generic remote was measured never to re-fetch a cached file.

### Deployment target

A self-hosted Artifactory Pro or higher, on Kubernetes or otherwise.
The limits of the instances used for testing are not design constraints.

## Research

Searched for existing tools before settling on writing one:

- **JFrog** has no feature for mirroring a non-Artifactory yum or apt repository.
  Pull replication requires an Artifactory source.
  Its air-gap guidance for third-party packages is dependency declarations run through remote repositories, or "dedicated scripts".
- **Pulp** (Satellite, Foreman/Katello) is the closest finished product, with bit-for-bit rpm mirroring and verbatim deb publication,
  but keeps its own storage and has its own air-gap export.
- **rclone** can stream an HTTP mirror into Artifactory WebDAV.
  One unresolved report of wrong listings from Artifactory WebDAV through rclone lowered confidence in it.
- No community tool was found that syncs a distro mirror into Artifactory.

Sources are listed in [design.md](design.md#sources).

## Test environment

Testing needs a licensed instance.
Artifactory OSS lacks Copy Item and Artifact Sync Download, which JFrog marks as Pro only.
An instance with an expired licence still answers metadata calls (repository listing and creation, AQL) but returns 503 `Trial license expired!` on every artifact read or write.
Everything in [verification.md](verification.md) ran against a JFrog Cloud instance, which serves the same REST API as a self-hosted one.

## Open questions

- **PyPI and npm index.** Confirm that an Artifactory-built index is acceptable, given the package files stay identical.
- **Scope at full scale.** Which yum and apt repositories. This sets storage and the first-sync API call count.
