"""Settings from config.toml, and secrets from .env, in the project directory."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_FILE = "config.toml"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(Exception):
    pass


@dataclass
class ProfileConfig:
    resume_doc: str = ""
    behavioral_doc: str = ""
    preferences: str = ""
    extra_keywords: list[str] = field(default_factory=list)


@dataclass
class FilterConfig:
    title_include: list[str] = field(default_factory=lambda: ["engineer", "developer"])
    title_exclude: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=lambda: ["new york"])
    allow_remote: bool = True
    remote_exclude: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)


@dataclass
class SearchConfig:
    sites: list[str] = field(default_factory=lambda: ["indeed", "linkedin"])
    results_per_site: int = 25
    hours_old: int = 72
    distance: int = 25
    fetch_descriptions: bool = True


@dataclass
class LoopConfig:
    name: str
    search: str
    location: str = "New York, NY"
    remote: bool = False
    sites: list[str] | None = None
    results_per_site: int | None = None
    hours_old: int | None = None
    loopcv_location: str = ""


@dataclass
class CompaniesConfig:
    greenhouse: list[str] = field(default_factory=list)
    lever: list[str] = field(default_factory=list)
    ashby: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    score_model: str = "claude-opus-5-5"
    writer_model: str = "claude-opus-5-5"
    score_effort: str = "low"
    writer_effort: str = "high"
    max_jobs_scored_per_run: int = 60
    min_score: int = 70
    concurrency: int = 4


@dataclass
class LoopcvConfig:
    level: str = "Associate"
    job_type: str = "Full-time"
    scrapers: list[str] = field(default_factory=list)


@dataclass
class Config:
    root: Path
    profile: ProfileConfig
    filters: FilterConfig
    search: SearchConfig
    loops: list[LoopConfig]
    companies: CompaniesConfig
    llm: LLMConfig
    loopcv: LoopcvConfig

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"


SECTIONS = {
    "profile": ProfileConfig,
    "filters": FilterConfig,
    "search": SearchConfig,
    "companies": CompaniesConfig,
    "llm": LLMConfig,
    "loopcv": LoopcvConfig,
}


def find_root() -> Path:
    if home := os.environ.get("JOBHELPER_HOME"):
        return Path(home).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for directory in (cwd, *cwd.parents):
        if (directory / CONFIG_FILE).is_file():
            return directory
    raise ConfigError(
        f"No {CONFIG_FILE} in {cwd} or its parents. "
        "Run jobhelper from the project directory, or set JOBHELPER_HOME."
    )


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines into the environment; variables that are already set win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)


def _section(cls, raw: dict, name: str):
    unknown = set(raw) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigError(f"Unknown key(s) in [{name}]: {', '.join(sorted(unknown))}")
    try:
        return cls(**raw)
    except TypeError as e:  # a required key is missing
        raise ConfigError(f"[{name}]: {e}") from None


def load_config(root: Path | None = None) -> Config:
    root = root or find_root()
    load_dotenv(root / ".env")
    try:
        raw = tomllib.loads((root / CONFIG_FILE).read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{CONFIG_FILE}: {e}") from None

    unknown = set(raw) - set(SECTIONS) - {"loops"}
    if unknown:
        raise ConfigError(f"Unknown section(s) in {CONFIG_FILE}: {', '.join(sorted(unknown))}")
    loops = [_section(LoopConfig, loop, "loops") for loop in raw.get("loops", [])]
    if len({loop.name for loop in loops}) != len(loops):
        raise ConfigError("Each [[loops]] entry needs a unique name")

    sections = {name: _section(cls, raw.get(name, {}), name) for name, cls in SECTIONS.items()}
    cfg = Config(root=root, loops=loops, **sections)
    for name in ("score_effort", "writer_effort"):
        if getattr(cfg.llm, name) not in EFFORTS:
            raise ConfigError(f"[llm] {name} must be one of: {', '.join(EFFORTS)}")
    return cfg
