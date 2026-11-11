"""Capability profiles for controlled Agent ablation experiments."""

from dataclasses import dataclass
from enum import StrEnum


class AgentProfile(StrEnum):
    BASIC = "basic"
    CODE_INTELLIGENCE = "code_intelligence"
    PLANNER = "planner"
    FULL = "full"


@dataclass(frozen=True)
class AgentCapabilities:
    code_intelligence: bool
    repair_planner: bool
    repair_loop: bool

    def as_dict(self) -> dict[str, bool]:
        return {
            "code_intelligence": self.code_intelligence,
            "repair_planner": self.repair_planner,
            "repair_loop": self.repair_loop,
        }


PROFILE_CAPABILITIES: dict[AgentProfile, AgentCapabilities] = {
    AgentProfile.BASIC: AgentCapabilities(
        code_intelligence=False,
        repair_planner=False,
        repair_loop=False,
    ),
    AgentProfile.CODE_INTELLIGENCE: AgentCapabilities(
        code_intelligence=True,
        repair_planner=False,
        repair_loop=False,
    ),
    AgentProfile.PLANNER: AgentCapabilities(
        code_intelligence=True,
        repair_planner=True,
        repair_loop=False,
    ),
    AgentProfile.FULL: AgentCapabilities(
        code_intelligence=True,
        repair_planner=True,
        repair_loop=True,
    ),
}

DEFAULT_AGENT_PROFILE = AgentProfile.FULL
FULL_CAPABILITIES = PROFILE_CAPABILITIES[DEFAULT_AGENT_PROFILE]


def resolve_profile(
    profile: AgentProfile | str | None = None,
) -> AgentProfile:
    if profile is None:
        return DEFAULT_AGENT_PROFILE
    try:
        return AgentProfile(profile)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in AgentProfile)
        raise ValueError(f"未知 Agent profile: {profile!r}（可选: {allowed}）") from exc


def capabilities_for(
    profile: AgentProfile | str | None = None,
) -> AgentCapabilities:
    return PROFILE_CAPABILITIES[resolve_profile(profile)]
