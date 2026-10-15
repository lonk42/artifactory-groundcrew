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
  The role has since been removed.

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

### 2026-10-07, Groundcrew 0.3.0: PyPI

New JFrog Cloud instance, Artifactory 7.172.6.
Groundcrew created a PyPI remote on `files.pythonhosted.org`, a PyPI local and a virtual.
Mirror: `requests==2.32.3` and `pydantic>=2.10,<2.11`, for Python 3.11 `manylinux_2_28_x86_64` and Python 3.12 `manylinux_2_28_aarch64`.

- **First sync.** 11 wheels in 14 s, both platforms' `pydantic_core` and `charset_normalizer` among them.
  The next cycle planned 0 fetches: every local file's sha256 matched the hash pip reported from PyPI.
- **Client, against the local repository only.**
  `python:3.11-slim` installed `requests 2.32.3` and `pydantic 2.10.6` with all seven dependencies.
  `requests==2.31.0` failed with `from versions: 2.32.3`.
- **Removing a requirement.** Dropping `pydantic` deleted its 5 wheels, and its index pages returned 404 with no reindex call.
  The `.pypi/` index files Artifactory keeps in the local repository were left alone.
- **Unresolvable requirement.** The cycle ended `enumeration-failed` with pip's error, deleted nothing, and logged no credentials.

Found by the live run:

- **The remote serves no PEP 658 metadata**, so pip downloads each wheel to read its dependencies.
  That download caches it in the remote.
- **Cache paths.** pip fetches `api/pypi/<remote>/packages/packages/<hash path>`, cached at `<hash path>`.
  Sync Download on `<hash path>` is a 404 until it is cached, and on `packages/<hash path>` caches a second copy under that prefix.
  Groundcrew fetches anything pip has not cached through pip's route.
- **pip does not widen `--platform`.** Given `manylinux_2_28_x86_64` it refused every `manylinux_2_17` wheel, so Groundcrew passes each older tag as well.
- **pip authenticates with basic auth only.** Artifactory accepts an access token as the password with an empty username, and refuses it with any username but the token's own.

### 2026-10-08, Groundcrew 0.3.0: tier 2 demand

Same instance and `pypi` mirror.

- **Pull from another platform.** `MarkupSafe==3.0.2` for Python 3.13 on `macosx_11_0_arm64`, downloaded with `--no-deps` through the virtual.
  The next cycle found it, and an earlier pull of `six`, as demand.
  It copied the Mac wheel and resolved `markupsafe==3.0.2` for both Linux targets: 4 files, 4 s.
- **Provenance.** `groundcrew provenance pypi` listed all 4 as `demand`, with the requirement, the user and the time.
  The Linux wheels carry the user whose pull caused them.
- **Client, against the local repository only.** `python:3.11-slim` installed `markupsafe 3.0.2` and `six 1.16.0`.
- **Next cycle.** `unchanged`: nothing new in the cache, and the list not yet due.
- **Properties.** Values with `=`, `,` and `;` round-trip through `set_properties`; the package index is unaffected.

Not shown live: excluding Groundcrew's own downloads.
Groundcrew and the test client shared one token, so the cycle above overrode Groundcrew's identity; unit tests cover the exclusion.
`/access/api/v1/tokens/me` returned the token's subject, ending in `/users/<name>`, which matches `created_by` on cached files.

### 2026-10-08, Groundcrew 0.3.0: npm

Same instance.
Groundcrew created an npm remote on `registry.npmjs.org`, an npm local and a virtual.
Mirror: `express@4.21.2` and `esbuild@0.24.2`, with `os: [linux]` and `cpu: [x64, arm64]`.

- **Resolution.** `npm install --package-lock-only` cached only package metadata (`.npm/<name>/package.json`) in the remote.
  The lockfile names every platform's optional package with its `os` and `cpu`: 25 for esbuild alone.
- **First sync.** 72 tarballs in 15 s: express's 69, esbuild, and its `linux-x64` and `linux-arm64` packages.
- **Client, against the local repository only.** `node:22-slim` installed both, `require("express")` worked, and `npx esbuild --version` ran the native binary.
  `express@4.21.1` failed with `ETARGET`.
- **Demand.** `chalk@5.3.0` and `debug@4.4.0` pulled through the virtual were copied and tagged; `debug`'s dependency `ms` was already mirrored.
  Groundcrew's identity was overridden for this cycle, as in the PyPI demand test.
- **Image.** The image built with `node` and `npm` from `node:22-slim`, 305 MB; `plan --full` inside it resolved both mirrors.

Found by the live run:

- **Sync Download works for npm tarballs** at the path after `/api/npm/<remote>/`, which is where the remote caches them. PyPI needed its own download route.
- **The local npm index is built on copy,** as for PyPI, with no reindex call.

### 2026-10-08, Groundcrew 0.3.0: Docker

Same instance.
Groundcrew created a Docker remote on `registry-1.docker.io`, a Docker local and a virtual.
Mirror: `library/busybox:1.37.0`, platforms `linux/amd64` and `linux/arm64`.

- **Layout, through the remote's registry API.** The index cached at `library/busybox/1.37.0/list.manifest.json`, and each platform at `library/busybox/sha256__<digest>/` with its blobs.
  A single-platform tag (`hashicorp/http-echo:0.2.3`) cached its manifest and blobs in `<image>/<tag>/`.
  Each stored manifest's sha256 is its digest.
- **Blobs without the bytes.** A `HEAD` on a blob through the remote cached the whole blob (2.5 MB).
- **First sync.** 13 files, 4 MB, in 10 s: the index, each platform's manifest and two blobs, and each platform's attestation manifest and its blobs.
  A forced second cycle fetched nothing.
- **Clients, against the local repository only.** `docker pull` of each platform succeeded, with Docker Hub's digest `sha256:bdf57e52...`, and the amd64 image ran.
  `linux/s390x` failed with `manifest unknown`.
- **Demand.** An amd64 pull of `library/alpine:3.20` through the virtual cached 4 files.
  The next cycle copied them and resolved the tag for both platforms, 15 files, all tagged `demand`; the arm64 image then pulled from the local repository.
  Groundcrew's identity was overridden for this cycle, as in the PyPI demand test.

Found by the live run:

- **Sync Download and plain GETs do not work on a Docker remote**, and their 404 is cached for `missedRetrievalCachePeriodSecs` (1800 s).
  After one, the registry API also answered `MANIFEST_UNKNOWN` for that manifest; another platform of the same index still worked.
  Groundcrew uses only the registry API on Docker remotes.
- **The registry API takes the access token as a bearer token**, at `<artifactory>/api/docker/<repo>/v2/`.

### 2026-10-09, Groundcrew 0.3.1: Helm chart on Kubernetes

Same instance, with Groundcrew deployed by its Helm chart through Argo CD on a k3s cluster, token inline in the values.
Mirrors: a Debian `bookworm-updates` apt mirror, and the `pypi`, `npm` and `docker` mirrors above, unchanged.

- **First cycle.** Created the three Debian repositories and found the other nine.
  Debian copied 38 packages in 18 s; the tier 2 mirrors had nothing new to fetch.
- **List edit through git.** `click==8.1.7`, `lodash@4.17.21` and `library/alpine:3.21` added in the values.
  A cycle in the pod resolved and copied each: 1 wheel, 1 tarball and 15 Docker files, all tagged `config`.
- **Clients, pods on the cluster, against the local repository only.**
  `python:3.11-slim` installed `click 8.1.7` and `requests 2.32.3`; `click==8.1.6` failed with `from versions: 8.1.7`.
  `node:22-slim` installed `lodash 4.17.21` and `express 4.21.2`; `lodash@4.17.20` failed with `ETARGET`.
  The kubelet pulled `<host>/docker/library/alpine:3.21` as a pod image, with Docker Hub's digest `sha256:ce64758a...`.

Found by the run, and fixed in 0.4.0:

- **An edited list waited for the interval.** The daemon decides a mirror is due by time alone.
  The new pod started after the values changed, saw cycles from 3 minutes earlier, and idled; the cycles above were run with `groundcrew sync` in the pod.
  A mirror whose fingerprint differs from its last full cycle is now due at once, unless that cycle failed, when it waits for `retry_seconds`.
- **Failures were not logged.** A cycle ended `failed` with a count and no paths, after a `progress` line still reading `failed=0`.
  Each failure is now logged as `item.failed` with its path and error, each stage ends with `stage.done`, and `cycle.done` carries the count.

### 2026-10-12, Groundcrew 0.4.0: Docker tag rules

Same instance, now Artifactory 7.173.4; ad hoc CLI runs, not yet on Kubernetes.

- **Tag lists through a remote.** `tags/list` on the Docker Hub remote matched Docker Hub for `library/busybox` (219 tags), `library/hello-world` (10, never cached) and `library/node` (9,170).
  Nothing was stored in the remote cache.
  With `?n=100`, the `Link` header names `/v2/...?last=<tag>&n=100`, which Artifactory answers 404; the same query on `/api/docker/<remote>/v2/` returned the next page.
- **ghcr.** A Docker remote on `https://ghcr.io/` listed all 9 tags of a public image, sorted, and paginated the same way.
- **Rule.** `match: '\d+\.\d+\.\d+'`, `from: '0.2.0'`, platform `linux/amd64` selected `0.2.0`, `0.3.0` and `0.3.1`, 48 files.
  The next cycle was `unchanged`.
- **Client, against the local repository only.** `docker pull --platform linux/amd64` of `0.3.1` succeeded with ghcr's digest `sha256:ea3d10ef...`.

Found by the live run:

- **A blob shared by several manifests of one image is cached once.** The other manifest folders in the remote cache hold an empty entry for it, and copying that fails with `Could not calculate repo path`.
  The local repository serves the blob by digest from any folder of the image, so Groundcrew now copies each blob once per image.


### 2026-10-15, tree enumeration against Rocky's file list

Enumeration only, against `dl.rockylinux.org` directly in place of a remote; not yet run through Artifactory.

- **File list.** `fullfiletimelist-rocky`: version 2, 75 MB, 722,214 files, 5,897 directories and 221 symlinks, with an empty checksum section.
- **Directory links.** `9` resolved to `9.8`, `10` to `10.2` and `8` to `8.10`, each by one read through the link.
  `9.0` to `9.7` and `10.0` to `10.1` hold only a `README.txt` pointing at the vault.
- **Selection.** Including `9/**`, `9.*/**`, `10/**`, `10.*/**` and the top-level keys, excluding images, live media, debug, source and every repository but BaseOS and AppStream:
  301,922 packages and 768 metadata files, 837 GB, with `9/` and `9.8/` each listing 57,634 AppStream and 16,569 BaseOS files.
- **Cost.** 148 reads of `repomd.xml` and signatures, and a peak of 283 MiB resident.
- **Memory of a whole cycle.** All of Rocky 9 and 10 but images, ISOs, live media, debug and source, 674,854 files, run through `run_cycle` with downloads and copies stubbed out.
  Enumeration reached 360 MiB.
  Execution peaked at 1,560 MiB, because every file of a stage was submitted to the thread pool at once.
  Submitting a bounded window of `workers * 4` instead brought the peak to 483 MiB.
  The chart's memory request is now 1Gi.

### 2026-10-15, Groundcrew 0.5.0: tree on Kubernetes

The published 0.5.0 image deployed by the Helm chart, memory request 1Gi, against a JFrog Cloud instance.
One tree mirror of Rocky's `extras` for 9 and 10, through `9`, `9.*`, `10` and `10.*`, with the vault READMEs and the top-level keys.

- **Links.** `9` resolved to `9.8`, `10` to `10.2` and `8` to `8.10` through the remote.
- **First cycle.** 220 files and 32 metadata files, 146 MB, in 75 seconds; `progress` every 5 seconds.
  `9/` and `9.8/` held the same 76 files with the same checksums, and `10/` and `10.2/` the same 42.
  Every non-metadata file carried `groundcrew.mtime`.
- **Clients, against the local repository only.** Clean `rockylinux/rockylinux:9` and `:10`, stock repositories and keys removed, `gpgcheck=1`, `repo_gpgcheck=1`, key `RPM-GPG-KEY-Rocky-<n>` from the mirror.
  Each installed `epel-release` from `extras` through both the link path and the minor release path.
- **Later cycles.** A second cycle was `unchanged`, and a full plan had nothing to fetch, publish or delete among 252 files in scope.
- **Memory.** The pod's cgroup peaked at 177 MiB over the first cycles of all five mirrors, and at 273 MiB with a CLI `sync` and full `plan` running in the same container.

### 2026-10-15, Groundcrew 0.5.0: tree at scale on Kubernetes

Same deployment, the tree widened to x86_64 BaseOS, AppStream and extras of 9 and 10.

- **First cycle.** 47,512 files, 188 GB, and 90 metadata files in 3 hours 18 minutes, none failed.
  `9/` and `9.8/` held the same 13,885 files with the same checksums, and `10/` and `10.2/` the same 10,034.
  The local repository used 91 GB.
- **Memory.** The cgroup peaked at 177 MiB, while the file list was parsed; downloads ran at 60 to 136 MiB.
- **Clients, against the local repository only.** Clean `rockylinux/rockylinux:9` and `:10`, stock repositories and keys replaced, `gpgcheck=1`, `repo_gpgcheck=1`, the key from the mirror: `dnf install httpd tmux` resolved and installed from BaseOS and AppStream.

Found by the run, and fixed in 0.5.1:

- **AQL results are capped, silently.** JFrog Cloud returns at most 500,000 rows, and a property projection is a row per property, not per file.
  These RPMs carry 13 to 26 properties each, so the `groundcrew.mtime` query returned 29,085 of 47,732 files, and the next full plan would have fetched the other 18,647 again.
  The mtime is now read with a projection of that one property, and every listing is paged with `sort`, `offset` and `limit`.
  A full plan then had nothing to fetch.
