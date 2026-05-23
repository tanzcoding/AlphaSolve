from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TierMapping:
    name: str
    tier_to_preset: dict[str, str]

    def preset_for(self, tier: str) -> str:
        try:
            return self.tier_to_preset[tier]
        except KeyError:
            raise KeyError(
                f"tier {tier!r} not defined in tier mapping {self.name!r}; "
                f"available tiers: {sorted(self.tier_to_preset)}"
            ) from None
