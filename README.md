# Artifactory Groundcrew

Synchronisation and population of public package repositories into JFrog Artifactory instances.

```
 upstream ──▶ remote repo ──copy──▶ local repo ──▶ clients
                   ▲                     │
                   └── groundcrew: list upstream, fetch what is new, prune what upstream removed
```

[Tested](docs/verification.md#results) against Artifactory 7.170.x, with Debian, Ubuntu, Fedora and openSUSE Build Service upstreams.

## What it does

Mirrors come in two tiers.

| Tier | Types | Content |
| --- | --- | --- |
| 1 | `yum`, `apt` | A Linux package repository, whole, served as upstream publishes it |
| 2 | `generic` | A curated list of files, rehosted at their upstream paths |

- **Mirrors.**
  yum and apt repositories are stored byte for byte in Generic local repositories, so clients verify upstream signatures with stock keys.
  [Detail](docs/goals.md#goals)
- **Formats.**
  yum reads gzip, xz, bzip2 and zstd metadata.
  apt covers suite archives and flat repositories (`deb URL /`), with by-hash indexes and the `cnf` and `dep11` indexes Ubuntu clients fetch.
- **One copy.**
  Artifactory fetches from upstream through a remote repository and copies into the local one; copies cost no storage.
  Groundcrew connects only to Artifactory, so it needs no internet access of its own.
- **Ordered updates.**
  Packages first, then metadata, then deletions.
  A cycle with a failed package publishes no metadata and deletes nothing.
- **Deletions follow upstream and the configuration.**
  Removing a path or list entry deletes its content.
- **Polling.**
  A cycle reads only `repomd.xml` or `InRelease` until upstream changes, and lists everything at least daily.
- **Repository creation.**
  Optional.
- **Operator CLI.**
  Status, cycle history, dry-run plans and manual cycles.

## Requirements

1. Artifactory Pro or higher.
   Copy and Artifact Sync Download are not in the OSS edition.
2. An admin-scoped access token, created in the UI under User Management.
3. Network access from Artifactory to each upstream.

## Deploy

The image is published at `ghcr.io/lonk42/artifactory-groundcrew` and the chart at `oci://ghcr.io/lonk42/charts/artifactory-groundcrew`.

```sh
helm install groundcrew oci://ghcr.io/lonk42/charts/artifactory-groundcrew \
  --version <chart-version> \
  --namespace artifactory \
  --set image.repository=ghcr.io/lonk42/artifactory-groundcrew \
  --set artifactory.url=http://artifactory:8082/artifactory \
  --set artifactory.token=<admin token> \
  -f mirrors.yaml
```

```yaml
# mirrors.yaml
config:
  create_repositories: true
  mirrors:
    - name: rocky9-baseos
      type: yum
      remote: rocky-remote
      local: rocky
      virtual: rocky-virtual
      upstream_url: https://dl.rockylinux.org/pub/rocky/
      scope: 9/BaseOS/
      yum:
        paths: [9/BaseOS/x86_64/os]
```

Clients use the local or virtual repository in place of the upstream URL.

Important notes:

- **Test clients against the local repository, not the virtual one.**
  On the source, the virtual repository serves files missing from the local one from the remote.
  An instance filled from the local repository has no remote.
- **A mirror owns everything under its `scope`.**
  Files there that it does not list are deleted.
  Two mirrors sharing a local repository need non-overlapping scopes, and the configuration is refused otherwise.
- **Deletions are unlimited.**
  A cycle deletes whatever upstream or the configuration no longer lists, however much that is.

## Configuration

`/etc/groundcrew/config.yaml`, overlaid by `GROUNDCREW_` environment variables, which take precedence.
Every setting is in [docs/configuration.md](docs/configuration.md).

## CLI

The daemon runs each mirror on its interval.
The CLI, run inside the pod, reads its state and runs a cycle on demand.

```sh
groundcrew mirrors                 # Configured mirrors, with their tier
groundcrew status                  # Last result per mirror
groundcrew cycles rocky9-baseos    # Recent cycles from the ledger
groundcrew plan rocky9-baseos      # What a cycle would do, changing nothing
groundcrew sync rocky9-baseos      # Run a cycle now
groundcrew repos --apply           # Create the repositories mirrors need
```

## Monitoring

Alert on the `parked` log event and on the age of each mirror's last successful cycle, not on restarts.

## Limitations

- Tier 1 mirrors a repository whole.
  Suites, components and architectures narrow an apt mirror; nothing selects individual packages.
- yum repositories using `xml:base` locations are refused.
- apt `*.diff/Index` files (pdiffs) are not mirrored.
  apt logs `Ign` for them and fetches the full index.
- Files deleted from a local repository manually are restored by the next daily full listing, not sooner.

## TODO

- Tier 2 types: docker lists through `regsync`, PyPI and npm lists.
- Example configurations for common upstreams (Rocky, Debian, Ubuntu, EPEL, vendor repositories).
- Metrics, including the age of each mirror's last successful cycle.
- End-to-end tests against a live instance, enabled by an environment variable.
- A distribution-scale mirror test, and throughput measured from a client in the same region as the instance.

## Documentation

| | |
|---|---|
| [docs/goals.md](docs/goals.md) | Design goals and tenets |
| [docs/configuration.md](docs/configuration.md) | Every setting |
| [docs/design.md](docs/design.md) | Options considered and why this design was chosen |
| [docs/background.md](docs/background.md) | How the requirements were arrived at |
| [docs/verification.md](docs/verification.md) | Tests run against a live instance, and their results |
