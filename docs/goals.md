# Design goals

Agreed 2026-09-23.

## Goals

1. **Served as-is.**
   yum and apt repositories go into Generic local repositories byte for byte, so upstream signatures verify with stock keys.
   Docker images keep their upstream digests.
   PyPI and npm are indexed by Artifactory over package files that stay identical.
2. **Two tiers.**
   Tier 1 is Linux package repositories, yum and apt, synced whole and served exactly as the source publishes them.
   A tier 1 mirror has no package selection; suites, components and architectures are the only narrowing.
   Tier 2 is everything else: docker, PyPI, npm and generic.
   Syncing a whole ecosystem is not feasible, so a list in git selects what is rehosted, and each entry brings its dependencies.
3. **Persistent** until upstream removes an item, or until the item is removed from its list.
   Removing a list entry deletes its content.
4. **Deletions follow upstream and the lists**, with no deletion brake.
   Upstream's metadata is the authority; see [background](background.md#deletions-follow-the-configuration-too).
5. **One copy.**
   Artifactory fetches from upstream through a remote repository and copies into the local repository.
   Groundcrew lists, compares, orders and prunes.
6. **Ordered updates.**
   Packages first, then metadata, then deletions.
   A cycle with any failed package publishes no metadata and deletes nothing.
7. **Unmirrored content returns 404.**
8. **Configuration in git.**
   Repository creation is optional and off by default.
   When enabled, Groundcrew creates the repositories its mirrors need on the instance it points at.
9. **Talks only to Artifactory.**
   Upstream metadata is read through the remote repository, so the pod needs no internet access.
10. **Uses existing tools where they fit.**
    `regsync` for docker, the real pip and npm clients for dependency resolution.

## Tenets

- **Polling, with each cycle costing in proportion to what changed.**
- **Idempotent.** Any cycle can be re-run. A 404 on delete is success.
- **Does not crash-loop.** An unreachable dependency idles the cycle and retries.
  A configuration error parks the process and restates the reason every 300 seconds.
  Alert on those events and on sync age, not on restart counts.
- **AQL rules.** Every query projects `repo`, `path` and `name`.
  Set membership is `$or` of `$eq`.
  Queries stay under 6000 characters.
- **Verified against real services.** Query tests run against a live Artifactory.
- **Stack.** Synchronous Python, `httpx`, `tenacity`, `structlog`, `pydantic-settings`.
  YAML config file with `GROUNDCREW_` environment variables taking precedence.
- **Packaging.** Image runs as uid 1030 with group-0 writable directories.
  Image and chart publish to ghcr on separate paths.
  Versions change in `pyproject.toml`, `__init__.py` and `Chart.yaml` together, before tagging.
- **Its own Deployment.** Groundcrew needs only the REST API, so it runs as its own pod, in Artifactory's namespace or elsewhere.
- **Almost stateless.** Upstream metadata and the local repository are both re-read every cycle, so there is no cursor or snapshot to lose.
  State is a cycle ledger and a lock.

## Reading metadata through a remote

A Generic remote never re-fetches a file it has cached.
Measured on Artifactory 7.170.0: with `retrievalCachePeriodSecs: 1`, a second request and an explicit Sync Download both returned the cached copy.
Deleting the file from the `-cache` repository made the next request fetch upstream.
Groundcrew therefore expires every metadata file from the cache before reading it.
Package files are never expired, since upstream never changes a package under the same name.

Creating a remote also requires `metadataRetrievalTimeoutSecs` to be no larger than `retrievalCachePeriodSecs`, or the request is refused with a 400.
