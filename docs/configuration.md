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
| `role` | `source` | `source` syncs mirrors. `destination` only creates repositories, for an instance filled by other means. |
| `create_repositories` | `false` | Create the local, remote and virtual repositories the mirrors name. |
| `state_dir` | `/var/groundcrew/state` | Ledger, per-mirror state and locks. |
| `workers` | `8` | Parallel requests per cycle. |
| `retry_seconds` | `600` | How soon a mirror whose last cycle failed is tried again. |
| `idle_seconds` | `60` | How often the daemon checks for due mirrors. |
| `ledger_keep_rows` | `5000` | Cycle rows kept in `cycles.jsonl`. |
| `log_level` | `INFO` | |
| `log_format` | `console` | `console` or `json`. |

## Mirrors

| Field | Default | Effect |
| --- | --- | --- |
| `name` | | Unique name, used by the CLI and the ledger. |
| `type` | | `yum` or `apt` (tier 1, a whole repository), or `generic` (tier 2, a list of files). A section of the same name holds its source. |
| `remote` | | Remote repository key. Artifactory fetches upstream through it. |
| `local` | | Generic local repository key. The mirror lives here. |
| `virtual` | | Optional virtual repository created over the local one, and the remote on the source. |
| `upstream_url` | | Upstream root. Only used to create the remote. |
| `scope` | whole repository | Prefix of the local repository this mirror owns. Unlisted files under it are deleted. |
| `interval_seconds` | `3600` | Time between cycles. |
| `enabled` | `true` | |

Paths are relative to the remote's root, and land at the same path in the local repository.

### `yum`

| Field | Effect |
| --- | --- |
| `paths` | Repository roots, each holding `repodata/`. |

### `apt`

| Field | Effect |
| --- | --- |
| `base` | The directory holding `dists/` and `pool/`, or for a flat repository, `Release` and `Packages`. |
| `flat` | `true` for a flat repository (`deb URL /`). |
| `suites` | For example `[bookworm, bookworm-updates]`. Not used when flat. |
| `components` | For example `[main]`. Not used when flat. |
| `architectures` | For example `[amd64]`. `all` is always included. For a flat repository, filters its one `Packages` index. |
| `extra_indexes` | Index directories besides `binary-<arch>`, default `[i18n, dep11, cnf]`, limited to the chosen architectures. apt fails its update on a 404 for a listed `cnf` index once `command-not-found` is installed. |

### `generic`

| Field | Effect |
| --- | --- |
| `paths` | Files to mirror. |
