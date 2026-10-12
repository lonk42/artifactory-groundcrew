"""Settings: a YAML file overlaid by ``GROUNDCREW_`` environment variables.

Environment variables take precedence over the file, so a value set on the
Deployment is never masked by the ConfigMap.
Mirrors are only read from the file; they are structured and do not fit env.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("/etc/groundcrew/config.yaml")


def _dir(p: str) -> str:
    """Normalise a repository path prefix: no leading slash, one trailing slash."""
    p = p.strip().lstrip("/")
    return p if not p or p.endswith("/") else p + "/"


class YumSource(BaseModel):
    """yum/dnf repositories. Each path is a repository root holding ``repodata/``."""

    paths: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _norm(self) -> "YumSource":
        self.paths = [_dir(p) for p in self.paths]
        return self


class AptSource(BaseModel):
    """An apt archive.

    ``base`` holds ``dists/`` and ``pool/``. A flat repository (``deb URL /``)
    keeps Release and Packages directly in ``base`` instead, and has no suites
    or components; ``architectures`` then filters its single Packages index.
    """

    base: str = ""
    flat: bool = False
    suites: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    architectures: list[str] = Field(default_factory=list)
    # Index directories mirrored besides binary-<arch>, when Release lists them.
    # apt fetches cnf once command-not-found is installed (Ubuntu's default) and
    # dep11 with appstream, and a 404 on a listed cnf index fails the update.
    extra_indexes: list[str] = Field(default_factory=lambda: ["i18n", "dep11", "cnf"])

    @model_validator(mode="after")
    def _norm(self) -> "AptSource":
        self.base = _dir(self.base)
        if self.flat and (self.suites or self.components):
            raise ValueError("a flat apt repository has no suites or components")
        if not self.flat and not (self.suites and self.components and self.architectures):
            raise ValueError("apt needs suites, components and architectures unless flat: true")
        return self


class GenericSource(BaseModel):
    """An explicit list of files."""

    paths: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _norm(self) -> "GenericSource":
        self.paths = [p.strip().lstrip("/") for p in self.paths]
        return self


class PypiTarget(BaseModel):
    """One interpreter and platform the mirrored wheels must install on."""

    python: str = Field(pattern=r"^3\.\d+$")
    # Wheel platform tags, for example manylinux_2_28_x86_64. A manylinux or
    # musllinux tag also admits every older one, as an installer on that
    # platform would. Pure-Python wheels are always included.
    platforms: list[str] = Field(min_length=1)


class PypiSource(BaseModel):
    """A list of requirements, each resolved with its dependencies by pip.

    Each entry is resolved on its own for each target, so one entry that cannot
    be resolved does not change what another one brings. With no targets,
    a package clients pull is copied as pulled, without resolving it.
    """

    requirements: list[str] = Field(default_factory=list)
    targets: list[PypiTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def _targets(self) -> "PypiSource":
        if self.requirements and not self.targets:
            raise ValueError("pypi requirements need at least one target to resolve for")
        return self


class NpmSource(BaseModel):
    """A list of package specs, each resolved with its dependencies by npm.

    npm's lockfile names every platform's optional packages. ``os`` and ``cpu``
    keep only those for the listed platforms; empty keeps all.
    """

    packages: list[str] = Field(default_factory=list)
    os: list[str] = Field(default_factory=list)
    cpu: list[str] = Field(default_factory=list)


class DockerFollow(BaseModel):
    """A rule selecting tags of one image from those upstream publishes.

    Tags matching ``match`` are put in natural-sort order; ``from`` keeps those
    at or after it, then ``newest`` keeps the last N.
    """

    model_config = ConfigDict(populate_by_name=True)

    image: str
    match: str
    from_: str = Field(default="", alias="from")
    newest: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _rule(self) -> "DockerFollow":
        if ":" in self.image.rpartition("/")[2] or "@" in self.image:
            raise ValueError(f"docker follow image {self.image!r} takes no tag; match selects the tags")
        if "/" not in self.image:
            raise ValueError(f"docker follow image {self.image!r} needs its namespace; Docker Hub official images are library/{self.image}")
        try:
            re.compile(self.match)
        except re.error as exc:
            raise ValueError(f"docker follow match {self.match!r} is not a valid regex: {exc}") from None
        if not self.from_ and self.newest is None:
            raise ValueError(f"docker follow for {self.image!r} needs from or newest, or it selects every matching tag ever published")
        return self


class DockerSource(BaseModel):
    """A list of image tags, each copied with the manifests and blobs it names.

    The list may be empty, leaving only what clients pull through the remote.
    ``follow`` adds the tags each rule selects. ``platforms`` keeps only those
    entries of a multi-platform image, as ``os/architecture`` or
    ``os/architecture/variant``; empty keeps all.
    """

    images: list[str] = Field(default_factory=list)
    follow: list[DockerFollow] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _refs(self) -> "DockerSource":
        for ref in self.images:
            name, _, tag = ref.rpartition(":")
            if not name or "/" in tag or "@" in ref:
                raise ValueError(f"docker image {ref!r} needs a tag, as name:tag")
            if "/" not in name:
                raise ValueError(f"docker image {ref!r} needs its namespace; Docker Hub official images are library/{name}")
        return self


# Tier 1 mirrors a Linux package repository whole and serves it exactly as
# upstream publishes it; there is no selecting within one. Tier 2 rehosts a
# curated selection where a whole ecosystem is not feasible.
TIERS = {"yum": 1, "apt": 1, "generic": 2, "pypi": 2, "npm": 2, "docker": 2}


class Mirror(BaseModel):
    name: str
    type: Literal["yum", "apt", "generic", "pypi", "npm", "docker"]
    remote: str
    local: str
    upstream_url: str = ""
    virtual: str = ""
    # The prefix of the local repository this mirror owns. Every file under it
    # that the mirror does not list is deleted, which is how removing a list
    # entry removes its content. Empty means the whole repository.
    scope: str = ""
    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=60)
    yum: YumSource | None = None
    apt: AptSource | None = None
    generic: GenericSource | None = None
    pypi: PypiSource | None = None
    npm: NpmSource | None = None
    docker: DockerSource | None = None

    @model_validator(mode="after")
    def _section_matches_type(self) -> "Mirror":
        # These sections' fields all have defaults, so the section may be left out.
        if self.type == "pypi" and self.pypi is None:
            self.pypi = PypiSource()
        if self.type == "npm" and self.npm is None:
            self.npm = NpmSource()
        if self.type == "docker" and self.docker is None:
            self.docker = DockerSource()
        sections = {"yum": self.yum, "apt": self.apt, "generic": self.generic, "pypi": self.pypi,
                    "npm": self.npm, "docker": self.docker}
        if sections[self.type] is None:
            raise ValueError(f"mirror {self.name!r} has type {self.type!r} but no {self.type}: section")
        extra = [k for k, v in sections.items() if v is not None and k != self.type]
        if extra:
            raise ValueError(f"mirror {self.name!r} has type {self.type!r} but also {extra} sections")
        if self.upstream_url and not self.upstream_url.endswith("/"):
            self.upstream_url += "/"
        self.scope = _dir(self.scope)
        # A path outside the scope would sync, but nothing would ever delete
        # its files once it left upstream or the list.
        if self.tier == 2:
            if self.scope:
                raise ValueError(f"mirror {self.name!r} is tier 2, which deletes nothing, so scope has no effect; remove it")
            return self
        if self.apt is not None:
            roots = [self.apt.base]
        else:
            roots = self.yum.paths
        outside = [p or "/" for p in roots if not p.startswith(self.scope)]
        if outside:
            raise ValueError(f"mirror {self.name!r} has paths outside its scope {self.scope!r}: {outside}")
        return self

    @property
    def tier(self) -> int:
        return TIERS[self.type]

    def fingerprint(self) -> str:
        """Hash of everything that decides what the mirror contains.

        A change means a previous cycle's result no longer describes the
        wanted state, so the up-to-date short cut must not apply.
        """
        body = self.model_dump(include={"type", "remote", "local", "scope", "yum", "apt", "generic", "pypi", "npm", "docker"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROUNDCREW_", extra="forbid")

    artifactory_url: str = "http://localhost:8081/artifactory"
    artifactory_token: str = ""
    artifactory_token_file: str = ""
    artifactory_username: str = ""
    artifactory_password: str = ""
    artifactory_ca_cert: str = ""

    create_repositories: bool = False

    state_dir: Path = Path("/var/groundcrew/state")
    workers: int = Field(default=8, ge=1, le=64)
    idle_seconds: int = Field(default=60, ge=1)
    # How soon a mirror whose last cycle did not succeed is tried again.
    retry_seconds: int = Field(default=600, ge=60)
    ledger_keep_rows: int = Field(default=5000, ge=100)

    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    mirrors: list[Mirror] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "Settings":
        names = [m.name for m in self.mirrors]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate mirror names: {dupes}")
        for i, a in enumerate(self.mirrors):
            for b in self.mirrors[i + 1:]:
                # Tier 2 deletes nothing, so only a tier 1 mirror can delete another's files.
                if a.tier == b.tier == 2:
                    continue
                if a.local == b.local and (a.scope.startswith(b.scope) or b.scope.startswith(a.scope)):
                    raise ValueError(
                        f"mirrors {a.name!r} and {b.name!r} share local repository {a.local!r} with "
                        f"overlapping scopes {a.scope or '/'!r} and {b.scope or '/'!r}; each would delete the other's files"
                    )
        self.artifactory_url = self.artifactory_url.rstrip("/")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # The YAML file arrives as init kwargs; demote it below env.
        return env_settings, dotenv_settings, file_secret_settings, init_settings

    def mirror(self, name: str) -> Mirror:
        for m in self.mirrors:
            if m.name == name:
                return m
        raise KeyError(f"no mirror named {name!r}; configured: {[m.name for m in self.mirrors]}")


def load(path: Path | None = None) -> Settings:
    path = path or DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    return Settings(**data)
