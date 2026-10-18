"""Launch profiles for embedded terminal panels.

A profile names the command a panel runs. Built-in profiles cover OMP, PrimePi,
PowerShell and Command Prompt; users can add custom profiles. Profiles never
modify the agent installations they launch.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field


@dataclass
class LaunchProfile:
    profile_id: str
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    builtin: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> LaunchProfile:
        argv = [str(part) for part in data.get("argv", []) if str(part)]
        env = {str(k): str(v) for k, v in dict(data.get("env", {})).items()}
        return LaunchProfile(
            profile_id=str(data["profile_id"]),
            name=str(data.get("name", data["profile_id"])),
            argv=argv,
            env=env,
            builtin=bool(data.get("builtin", False)),
        )


BUILTIN_PROFILES: tuple[LaunchProfile, ...] = (
    LaunchProfile("omp", "OMP", ["omp"], builtin=True),
    LaunchProfile("primepi", "PrimePi", ["primepi"], builtin=True),
    LaunchProfile("powershell", "PowerShell", ["powershell.exe", "-NoLogo"], builtin=True),
    LaunchProfile("cmd", "Command Prompt", ["cmd.exe"], builtin=True),
)


def default_profiles() -> list[LaunchProfile]:
    return [LaunchProfile.from_dict(p.to_dict()) for p in BUILTIN_PROFILES]


def find_profile(profiles: list[LaunchProfile], profile_id: str) -> LaunchProfile | None:
    return next((p for p in profiles if p.profile_id == profile_id), None)


def resolve_argv(argv: list[str]) -> tuple[list[str], str]:
    """Resolve argv[0] on PATH. Returns (argv, error); error is empty when found."""
    if not argv:
        return argv, "Profile has no command"
    executable = shutil.which(argv[0])
    if executable is None:
        return argv, f"'{argv[0]}' was not found on PATH"
    return [executable, *argv[1:]], ""
