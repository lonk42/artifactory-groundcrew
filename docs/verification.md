# Verification

Tests against live Artifactory instances.
The first set checked the assumptions in [design.md](design.md) before any code was written; later results test Groundcrew itself.
Use small real repositories, such as the docker-ce yum and apt repositories.

The pair: `source` holds the remote and local repositories, `destination` only local repositories with the same keys.

## Before starting

The instances need a valid Pro licence.
On an expired licence every artifact upload and download returns 503 `Trial license expired!`,
while `/api/system/ping` still returns 200.

## Tests

- [x] **Sync Download caches a file.**
  `GET /api/download/<yum-remote>/<path>?content=none` on the source, then find the file in `<yum-remote>-cache` with AQL.
- [x] **Copy keeps the upstream path.**
  `POST /api/copy/<yum-remote>-cache/<path>?to=/<generic-local>/<path>`.
  `dnf` and `apt` install from the Generic local on the source with stock upstream keys.
- [ ] **Throughput.**
  Time per file for Sync Download plus copy, extrapolated to a full distribution.
  Figures below are from a client on another continent to the instance; still needs a run from a client in the same region.
- [x] **Docker.**
  `regsync` into a Docker local repository.
  A `docker pull` by digest on the destination matches upstream.
- [x] **PyPI and npm.**
  `pip download` and `npm install` through the remotes, copy to local, reindex.
  Installs work on the destination, and the index lists the copied files.
- [ ] **Optional: rclone into WebDAV** against a directory of 20,000 or more files, to settle option Y4.

## Results

### 2026-09-23, JFrog Cloud (Artifactory 7.170.0)

Upstream `download.docker.com`, through a Generic remote into a Generic local.
One package plus all metadata, for `centos/9/x86_64/stable` and `debian` `bookworm stable amd64`.

- **yum.** Clean `rockylinux:9`, repo pointed at the mirror only, `gpgcheck=1` and `repo_gpgcheck=1`, Docker's stock key.
  `dnf install docker-buildx-plugin` verified `repomd.xml.asc` and the package signature, and installed.
  `docker-ce-cli`, not mirrored, failed with `Cannot download, all mirrors were already tried without success`.
- **apt.** Clean `debian:bookworm`, `signed-by` Docker's stock key.
  `apt-get update` verified `InRelease` with no warnings, and `docker-buildx-plugin` installed.
  `docker-ce-cli`, not mirrored, returned 404.
- **Byte identity.** `repomd.xml`, `repomd.xml.asc` and the rpm match upstream by SHA-256.
- **Remote defaults.** A new remote has `unusedArtifactsCleanupPeriodHours: 0`, so the cache is never cleaned up by default.
- **Copy over an existing path** replaces the file.
  A stale `repomd.xml` deployed to the local repo was overwritten by a copy from the cache, which is the metadata refresh case.
- **Timing.** About 1 s per Sync Download and 1 s per copy, from a client on another continent.
  At 2 s per file serially, a 20,000-file first sync is about 11 hours, so the tool needs parallel requests.

Findings that change the tool:

- Docker's yum metadata is zstd-compressed (`primary.xml.zst`). The yum enumerator needs zstd as well as gzip.
- JFrog Cloud serves large files as a `302` to a signed S3 URL. Clients follow it, and so must any check that reads files back.
- Package paths in the Debian `Packages` index sit under `dists/<suite>/pool/`, not a top-level `pool/`. The enumerator takes paths from `Filename:` fields and assumes no layout.

Docker, with `regsync` from `docker.io/library/busybox`, tags `1.37.0` and `1.36.1`, into a Docker local repository:

- Both tags' digests match Docker Hub.
- All eight platforms and their attestation manifests were copied.
- `docker pull` and `docker run` from the mirror work.

PyPI, `pip download requests==2.32.3` through a PyPI remote, then copy from the cache into a PyPI local and reindex:

- The remote cache stores wheels under the `files.pythonhosted.org` hashed path (`f9/9b/<hash>/requests-2.32.3-py3-none-any.whl`).
  The local repository indexes them at the same path.
- The cached index `.pypi/requests.html` keeps upstream's absolute `files.pythonhosted.org` links.
  Artifactory rewrites links when serving, not when caching.
- The local index lists only the copied files, with the same `#sha256=` as upstream.
- `pip install requests==2.32.3` from the local repository alone works.
  `requests==2.31.0` fails with `from versions: 2.32.3`.

npm, `npm install express@4.21.2` through an npm remote (69 packages), then copy the tarballs into an npm local and reindex:

- The cached packument `.npm/<name>/package.json` keeps upstream's `registry.npmjs.org` tarball URLs, so npm is also rewritten when serving.
- The local packument lists only the copied version, and its `integrity` matches npmjs.org.
- `npm install express@4.21.2` from the local repository alone works.
  `express@4.21.1` fails with `notarget`.
- One dependency tarball missing from the local repository fails the whole install.
  A list entry is only usable once its full dependency closure is mirrored.

Rocky Linux 9 BaseOS, through a Generic remote on `https://dl.rockylinux.org/pub/rocky/` into a Generic local:

- Upstream today: 2,866 packages, 14.6 GB, nine metadata files plus `repomd.xml` and `repomd.xml.asc`.
  `primary.xml.gz` is 29 MB.
- Slice: every metadata file, the 7 packages `dnf download --urls --resolve tmux lsof zip unzip rsync` names against a clean `rockylinux:9`, and 93 other packages chosen at random.
- 100 packages in 31 s with 8 parallel workers, 3.2 files/s, no failures.
  The 11 metadata files took 8 s, with `repomd.xml` and its signature last.
  At that rate the API work for all of BaseOS is about 15 minutes.
- Clean `rockylinux:9`, every repo disabled except the mirror, `gpgcheck=1`, `repo_gpgcheck=1`, key `file:///etc/pki/rpm-gpg/RPM-GPG-KEY-Rocky-9` from the image.
  `dnf install tmux lsof zip unzip rsync` verified the repository and package signatures and installed, and upgraded `libacl` as a dependency.
  `strace`, in the metadata but not mirrored, failed with `Cannot download`.
- `dnf download --urls --resolve` against upstream gives a package list with its dependency closure, without downloading.
  It would fit a partial distribution mirror, which tier 1 does not do ([goals](goals.md#goals)).

Still open: throughput measured from a client in the same region as the instance.

### 2026-09-23, Groundcrew 0.1.0 against the same instance

Full mirrors of the openSUSE Build Service `shells:fish:release:4` project, which is real, signed and small:
`Fedora_42` (yum, 7 packages, 46 MB) and `Debian_12` (flat apt, filtered to amd64, 2 packages, 15 MB).
Groundcrew created the remote, local and virtual repositories itself.

- **First sync.** Fedora 7 packages and 6 metadata files in 19 s; Debian 2 packages and 6 metadata files in 10 s.
- **Unchanged upstream.** The next cycle read only `repomd.xml` and finished in 1.4 s as `unchanged`.
- **Clients, against the local repository only.**
  `fedora:42` with `gpgcheck=1`, `repo_gpgcheck=1` and the project's `repomd.xml.key` installed `fish 4.9.3-2.2` from the mirror.
  `debian:bookworm` with `signed-by` the project's `Release.key` verified `InRelease` and installed `fish 4.9.3-1`, with the mirror as its only source.
- **Container image.** Started with empty state, confirmed the three repositories, fully re-listed both mirrors and found nothing to fetch.
  The CLI works inside the container, and SIGTERM exits 0.
- **Configuration errors park.** A config missing required fields logged `parked reason="invalid configuration"` and kept the process up.

Found by the live run, and fixed:

- **Special characters in the copy target were double-encoded.**
  The target path is a query parameter, and quoting it before httpx encoded it again stored `shells:/fish:/...` as a literal `shells%3A/fish%3A/...`.
  Artifactory stores colons literally; the cache held the right paths.
  Clients still installed, because they used the virtual repository, which fell back to the remote on the source.
  Hence the rule to test clients against the local repository: an instance without the remote has nothing to fall back to.

### 2026-09-29, Groundcrew 0.1.0: suite apt, zstd yum and generic

Same instance. Groundcrew created every repository, and each client used the local repository as its only source.

| Mirror | Type | Upstream | Content |
| --- | --- | --- | --- |
| Debian | apt | `deb.debian.org/debian`, `bookworm-updates`, `main contrib non-free-firmware`, amd64 | 38 packages, 28 MB, by-hash, one empty component |
| Ubuntu | apt | `archive.ubuntu.com/ubuntu`, `noble-backports`, `restricted multiverse`, amd64 | 1 package, `cnf` and `dep11` |
| openh264 | yum | `codecs.fedoraproject.org/openh264/`, `42/x86_64` | 6 packages, `primary.xml.zst`, packages behind a redirect to another host |
| yq | generic | `github.com`, two release files | Redirected to GitHub's object store |

- **Debian.** `debian:bookworm`, `signed-by` the stock archive keyring.
  `apt-get update` verified `InRelease` through by-hash, and `libssl3` and `openssl` installed from the mirror.
- **pdiffs.** With the stored `Packages` made stale, apt tried `Packages.diff/Index`, logged `Ign` on the 404, fetched the full `Packages` and exited 0.
- **Ubuntu.** `ubuntu:24.04` with `command-not-found` and `appstream` installed from upstream, then pointed at the mirror.
  With `extra_indexes` at its old default of `[i18n]`, `apt-get update` failed:
  `E: Failed to fetch .../dists/noble-backports/restricted/cnf/Commands-amd64  404`.
  With `cnf` and `dep11` mirrored it fetched `Components` and `c-n-f Metadata` for both components and exited 0.
- **openh264.** `fedora:42` with `gpgcheck=1` and the image's `RPM-GPG-KEY-fedora-42-x86_64` installed `openh264 2.5.1-1.fc42`.
  Upstream publishes no `repomd.xml.asc`, so `repo_gpgcheck` was off.
- **yq.** Both files match GitHub by SHA-256.
  Dropping one from the list was refused by the deletion brake (`1 of 2 files (50.0%)`), since removed, then deleted with the brake raised, and the path returned 404.
- **Unchanged upstream.** Repeat cycles finished `unchanged` in 1.7 to 2.4 s for the three tier 1 mirrors.
- **Destination role.** Created the local repository and a virtual over it alone, and no remote.

Found by the live run, and fixed:

- **Identical index files raced.**
  Ubuntu's empty `dep11` icon tarballs hash the same, so several Release entries share one `by-hash/SHA256/<sha>` path.
  Parallel workers took the duplicate entries, and one expired the file from the cache while another copied it:
  `HTTP 400 Could not calculate repo path ... does not exist`.
  The plan now holds each path once.
- **Ubuntu clients needed `cnf`.** `extra_indexes` now defaults to `[i18n, dep11, cnf]`, limited to the chosen architectures.

Observed and left alone:

- **Transient upstream 404.** On the first openh264 cycle, 4 of 6 packages returned 404 through the remote, on a redirect chain from `codecs.fedoraproject.org` to `ciscobinary.openh264.org`.
  The cycle published no metadata, and the next one fetched them.
- **Absent optional indexes are retried.** Release files list uncompressed variants that upstream does not publish.
  Every full listing asks for them again: 30 of Ubuntu's 76 planned metadata files.
