from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    role_to_preset: dict[str, str]

    def preset_for(self, role: str) -> str:
        try:
            return self.role_to_preset[role]
        except KeyError:
            raise KeyError(
                f"role {role!r} not defined in profile {self.name!r}; "
                f"available roles: {sorted(self.role_to_preset)}"
            ) from None
