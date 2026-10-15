# Configuration

Settings come from `/etc/groundcrew/config.yaml` (or the path in `GROUNDCREW_CONFIG`).
Any top-level setting can be overridden by an environment variable named `GROUNDCREW_` plus the setting in upper case, which takes precedence over the file.
Mirrors are only read from the file.

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `artifactory_url` | `http://localhost:8081/artifactory` | Base URL, ending in `/artifactory`. |
| `artifactory_token` | | Admin-scoped bearer token. |
| `artifactory_token_file` | | File holding the token, re-read on every request. Outranks `artifactory_token`. |
| `artifactory_username`, `artifactory_password` | | Basic auth. Outranks both token settings when both are set. |
| `artifactory_ca_cert` | | PEM bundle file for a private CA. |
| `create_repositories` | `false` | Create the local, remote and virtual repositories the mirrors name: Generic for tier 1 and generic, typed for PyPI, npm and Docker, all with the `simple-default` layout. Existing repositories are left as they are. |
| `state_dir` | `/var/groundcrew/state` | Ledger, per-mirror state and locks. |
| `workers` | `8` | Parallel requests per cycle. |
| `retry_seconds` | `600` | How soon a mirror whose last cycle failed is tried again. |
| `idle_seconds` | `60` | How often the daemon checks for due mirrors. |
| `ledger_keep_rows` | `5000` | Cycle rows kept in `cycles.jsonl`. |
| `log_level` | `INFO` | |
| `log_format` | `console` | `console` or `json`. |

```yaml
artifactory_url: https://artifactory.example.com/artifactory
artifactory_token_file: /var/run/secrets/groundcrew/token
artifactory_ca_cert: /etc/ssl/private-ca.pem
create_repositories: true
workers: 16
log_format: json
mirrors: []
```

## Mirrors

| Field | Default | Effect |
| --- | --- | --- |
| `name` | | Unique name, used by the CLI and the ledger. |
| `type` | | `yum`, `apt` or `tree` (tier 1, a whole repository or tree), or `generic`, `pypi`, `npm` or `docker` (tier 2, a list). A section of the same name holds its source. |
| `remote` | | Remote repository key. Artifactory fetches upstream through it. |
| `local` | | Generic local repository key. The mirror lives here. |
| `virtual` | | Optional virtual repository created over the local one, and the remote on the source. |
| `upstream_url` | | Upstream root. Only used to create the remote. |
| `scope` | whole repository | Tier 1: prefix of the local repository this mirror owns. Unlisted files under it are deleted. Every path, or the apt `base`, must sit under it; a tree lists only files under it. Refused on tier 2, which deletes nothing. |
| `interval_seconds` | `3600` | Time between cycles. A change to what the mirror contains runs one at once. |
| `enabled` | `true` | |

Paths are relative to the remote's root, and land at the same path in the local repository.

Each section below has one example per variant.
The examples are entries of the `mirrors:` list.

### `yum`

| Field | Effect |
| --- | --- |
| `paths` | Repository roots, each holding `repodata/`. |

Two mirrors sharing one local repository, each owning its own `scope`:

```yaml
mirrors:
  - name: rocky9-baseos
    type: yum
    remote: rocky-remote
    local: rocky
    virtual: rocky-virtual
    upstream_url: https://dl.rockylinux.org/pub/rocky/
    scope: 9/BaseOS/
    yum:
      paths: [9/BaseOS/x86_64/os, 9/BaseOS/aarch64/os]
  - name: rocky9-appstream
    type: yum
    remote: rocky-remote
    local: rocky
    scope: 9/AppStream/
    interval_seconds: 21600
    yum:
      paths: [9/AppStream/x86_64/os]
```

### `apt`

| Field | Effect |
| --- | --- |
| `base` | The directory holding `dists/` and `pool/`, or for a flat repository, `Release` and `Packages`. |
| `flat` | `true` for a flat repository (`deb URL /`). |
| `suites` | For example `[bookworm, bookworm-updates]`. Not used when flat. |
| `components` | For example `[main]`. Not used when flat. |
| `architectures` | For example `[amd64]`. `all` is always included. For a flat repository, filters its one `Packages` index. |
| `extra_indexes` | Index directories besides `binary-<arch>`, default `[i18n, dep11, cnf]`, limited to the chosen architectures. apt fails its update on a 404 for a listed `cnf` index once `command-not-found` is installed. |

A suite archive:

```yaml
mirrors:
  - name: debian-bookworm
    type: apt
    remote: debian-remote
    local: debian
    upstream_url: https://deb.debian.org/
    scope: debian/
    apt:
      base: debian
      suites: [bookworm, bookworm-updates]
      components: [main, contrib, non-free-firmware]
      architectures: [amd64, arm64]
```

A suite archive with fewer extra indexes, for clients without `command-not-found` or appstream:

```yaml
mirrors:
  - name: ubuntu-noble
    type: apt
    remote: ubuntu-remote
    local: ubuntu
    upstream_url: http://archive.ubuntu.com/
    scope: ubuntu/
    apt:
      base: ubuntu
      suites: [noble, noble-updates, noble-security]
      components: [main, universe]
      architectures: [amd64]
      extra_indexes: [i18n]
```

A flat repository:

```yaml
mirrors:
  - name: cuda-ubuntu2404
    type: apt
    remote: nvidia-remote
    local: nvidia
    upstream_url: https://developer.download.nvidia.com/
    scope: compute/cuda/repos/ubuntu2404/x86_64/
    apt:
      base: compute/cuda/repos/ubuntu2404/x86_64
      flat: true
      architectures: [amd64]
```

### `tree`

Every file in an upstream tree that publishes a `quick-fedora-mirror` file list, as Rocky and Fedora do.

| Field | Effect |
| --- | --- |
| `filelist` | The list's path from the remote's root, for example `fullfiletimelist-rocky`. |
| `include` | Patterns a file's path must match one of. Empty includes everything. |
| `exclude` | Patterns that drop a file `include` selected. |

Patterns match the path from the remote's root.
A glob matches the whole path: `*` and `?` stay within one directory, `**` crosses directories, and `**/` also matches none.
A pattern starting `re:` is a Python regex, searched for anywhere in the path; anchor it with `^`.

A symlink to a file is mirrored as that file.
A symlink to a directory, such as Rocky's `9`, is mirrored as a copy of its target, so `9/` and `9.8/` are both served and stored once.
When upstream moves the link to a new minor release, the next cycle follows it and removes what upstream removed.
Files are compared by modification time, kept on each file as the `groundcrew.mtime` property.

Rocky 9 and 10 as upstream has them, with release keys, without images, ISOs, debug or source packages:

```yaml
mirrors:
  - name: rocky
    type: tree
    remote: rocky-remote
    local: rocky
    virtual: rocky-virtual
    upstream_url: https://dl.rockylinux.org/pub/rocky/
    tree:
      filelist: fullfiletimelist-rocky
      include: ["9/**", "9.*/**", "10/**", "10.*/**", "RPM-GPG-KEY-Rocky-*"]
      exclude: ["**/images/**", "**/isos/**", "**/live/**", "**/debug/**", "**/source/**"]
```

Only BaseOS and AppStream, with a regex:

```yaml
      exclude: ['re:^(9|10)[.0-9]*/(?!BaseOS/|AppStream/)[^/]+/', "**/debug/**", "**/source/**"]
```

### `generic`

| Field | Effect |
| --- | --- |
| `paths` | Files to mirror. A file a client pulls through the remote is mirrored too. |

```yaml
mirrors:
  - name: helm-binaries
    type: generic
    remote: helm-remote
    local: tools
    virtual: tools-virtual
    upstream_url: https://get.helm.sh/
    generic:
      paths:
        - helm-v3.16.2-linux-amd64.tar.gz
        - helm-v3.16.2-linux-amd64.tar.gz.sha256sum
```

### `pypi`

| Field | Effect |
| --- | --- |
| `requirements` | pip requirement strings, for example `requests==2.32.3` or `pydantic>=2.10,<2.11`. Each is resolved with its dependencies. Empty, or with no `pypi:` section, copies only what clients pull. |
| `targets` | One or more `{python, platforms}`. `python` is a version such as `"3.12"`; `platforms` are wheel platform tags such as `manylinux_2_28_x86_64`. A manylinux or musllinux tag includes every older one. Required when `requirements` is not empty. With none, a pulled package is copied as pulled, without resolving its dependencies. |

A list, resolved for two targets:

```yaml
mirrors:
  - name: pypi
    type: pypi
    remote: pypi-remote
    local: pypi
    virtual: pypi-virtual
    upstream_url: https://files.pythonhosted.org/
    pypi:
      requirements: ["requests==2.32.3", "pydantic>=2.10,<2.11"]
      targets:
        - python: "3.12"
          platforms: [manylinux_2_28_x86_64, manylinux_2_28_aarch64]
        - python: "3.11"
          platforms: [musllinux_1_2_x86_64]
```

No list; pulls are resolved for the targets:

```yaml
mirrors:
  - name: pypi
    type: pypi
    remote: pypi-remote
    local: pypi
    virtual: pypi-virtual
    upstream_url: https://files.pythonhosted.org/
    pypi:
      targets:
        - python: "3.12"
          platforms: [manylinux_2_28_x86_64]
```

No section; pulls are copied as pulled:

```yaml
mirrors:
  - name: pypi
    type: pypi
    remote: pypi-remote
    local: pypi
    virtual: pypi-virtual
    upstream_url: https://files.pythonhosted.org/
```

A package a client pulls through the remote is copied, and its `name==version` resolved for each target.
The remote, local and virtual repositories are PyPI repositories, and `upstream_url` is `https://files.pythonhosted.org/`.
Clients use `<artifactory_url>/api/pypi/<local>/simple` as their index.

### `npm`

| Field | Effect |
| --- | --- |
| `packages` | npm package specs, for example `express@4.21.2` or `esbuild@^0.24`. Each is resolved with its dependencies. Empty, or with no `npm:` section, copies only what clients pull. |
| `os` | Keep only optional packages for these operating systems, for example `[linux]`. Empty keeps all. |
| `cpu` | Keep only optional packages for these CPUs, for example `[x64, arm64]`. Empty keeps all. |

A list, keeping optional packages for Linux on x64 and arm64:

```yaml
mirrors:
  - name: npm
    type: npm
    remote: npm-remote
    local: npm
    virtual: npm-virtual
    upstream_url: https://registry.npmjs.org/
    npm:
      packages: ["express@4.21.2", "esbuild@^0.24"]
      os: [linux]
      cpu: [x64, arm64]
```

No section; only pulls, with every platform's optional packages:

```yaml
mirrors:
  - name: npm
    type: npm
    remote: npm-remote
    local: npm
    virtual: npm-virtual
    upstream_url: https://registry.npmjs.org/
```

A package a client pulls through the remote is copied, and its `name@version` resolved with the same filters.
The remote, local and virtual repositories are npm repositories, and `upstream_url` is `https://registry.npmjs.org/`.
Clients use `<artifactory_url>/api/npm/<local>/` as their registry.

### `docker`

| Field | Effect |
| --- | --- |
| `images` | Image tags with their namespace, for example `library/busybox:1.37.0`. Docker Hub official images are under `library/`. Empty, or with no `docker:` section, copies only what clients pull. |
| `follow` | Rules that select tags from those upstream lists for an image. See below. |
| `platforms` | Platforms to keep from a multi-platform image, as `os/architecture` or `os/architecture/variant`, for example `[linux/amd64, linux/arm64]`. Empty keeps all. Attestation manifests for kept platforms are kept. Applies to listed, followed and pulled tags. |

| `follow` field | Effect |
| --- | --- |
| `image` | The image with its namespace, and no tag. |
| `match` | A regex the whole tag must match. |
| `from` | Keep tags at or after this one in natural-sort order. It need not exist upstream. |
| `newest` | Keep the last N in natural-sort order, after `from`. |

`from` or `newest` is required.
Each cycle reads every rule's tag list, and a newly selected tag is copied in that cycle.
A tag that later falls outside `newest` stays in the local repository.

Natural-sort order compares digit runs as numbers and other runs as text, and a digit run sorts before text at the same position.
`1.10.0` follows `1.9.2`, and `16.4-alpine3.20` follows `16.4-alpine3.19`.

What each rule selects from an image tagged
`1`, `1.9.2`, `1.10.0`, `1.10.1`, `1.11`, `1.11.0`, `1.11.0-rc1`, `1.11.0-alpine`, `v1.11.0` and `latest`:

| `match` | `from` | `newest` | Selects |
| --- | --- | --- | --- |
| `\d+\.\d+\.\d+` | | `3` | `1.10.0`, `1.10.1`, `1.11.0` |
| `\d+\.\d+\.\d+` | `1.10` | | `1.10.0`, `1.10.1`, `1.11.0`, and each later release |
| `\d+\.\d+\.\d+` | `1.10` | `1` | `1.11.0`, then only the latest release |
| `1\.10\.\d+` | | `5` | `1.10.0`, `1.10.1`: one minor line |
| `\d+\.\d+\.\d+-alpine` | | `1` | `1.11.0-alpine`: one variant |
| `\d+\.\d+` | | `1` | `1.11`: the moving minor tag |
| `\d+\.\d+\.\d+(-rc\d+)?` | | `3` | `1.10.1`, `1.11.0`, `1.11.0-rc1` |
| `v?\d+\.\d+\.\d+` | | `2` | `1.11.0`, `v1.11.0` |

The last two rows show the order working against a rule:

- A pre-release sorts after its release, so `1.11.0-rc1` counts as newer than `1.11.0`.
  Leave pre-releases out of `match` unless you want them.
- A text run sorts after a digit run, so every `v`-prefixed tag counts as newer than every bare one.
  Match one form, `v\d+\.\d+\.\d+` or `\d+\.\d+\.\d+`, not both.
- `latest` and other words sort after every version.

Other schemes:

| Tags | `match` | `from` | `newest` | Selects |
| --- | --- | --- | --- | --- |
| `2024-11-30`, `2025-01-15`, `2025-02-01`, `2025-02-03`, `nightly-2025-02-02` | `\d{4}-\d{2}-\d{2}` | `2025-02` | | `2025-02-01`, `2025-02-03` |
| `16.3-alpine3.19`, `16.4-alpine3.19`, `16.4-alpine3.20`, `16.4-alpine`, `17.0-alpine3.20` | `16\.\d+-alpine3\.\d+` | | `1` | `16.4-alpine3.20` |

A list of tags from Docker Hub, for two platforms:

```yaml
mirrors:
  - name: dockerhub
    type: docker
    remote: dockerhub-remote
    local: dockerhub
    virtual: dockerhub-virtual
    upstream_url: https://registry-1.docker.io/
    docker:
      images: [library/busybox:1.37.0, library/alpine:3.21]
      platforms: [linux/amd64, linux/arm64]
```

Rules, alongside a listed tag: the newest five `16.x-alpine` releases of postgres, and every release of an image on ghcr from 0.2.0:

```yaml
mirrors:
  - name: dockerhub
    type: docker
    remote: dockerhub-remote
    local: dockerhub
    upstream_url: https://registry-1.docker.io/
    docker:
      images: [library/alpine:3.21]
      follow:
        - image: library/postgres
          match: '16\.\d+-alpine'
          newest: 5
      platforms: [linux/amd64]
  - name: ghcr
    type: docker
    remote: ghcr-remote
    local: ghcr
    upstream_url: https://ghcr.io/
    docker:
      follow:
        - image: lonk42/artifactory-groundcrew
          match: '\d+\.\d+\.\d+'
          from: '0.2.0'
```

No section; only pulls, for every platform:

```yaml
mirrors:
  - name: dockerhub
    type: docker
    remote: dockerhub-remote
    local: dockerhub
    virtual: dockerhub-virtual
    upstream_url: https://registry-1.docker.io/
```

An image a client pulls through the remote is copied, and its tag resolved for the same platforms.
Tags are resolved again daily, including those clients pulled, so a moved tag follows upstream.
The remote, local and virtual repositories are Docker repositories, and `upstream_url` is `https://registry-1.docker.io/` for Docker Hub.
Clients pull `<host>/<local>/<image>:<tag>`.
