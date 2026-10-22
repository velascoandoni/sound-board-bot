import math
import time
from dataclasses import dataclass, field


@dataclass
class SoundRow:
    """Represents one instance of a sound playback."""

    id: str
    name: str
    user_id: int
    duration_seconds: float
    emoji: str = "🔊"
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> int:
        return max(0, int(time.monotonic() - self.started_at))

    @property
    def remaining_seconds(self) -> int:
        elapsed = time.monotonic() - self.started_at
        return max(0, math.ceil(self.duration_seconds - elapsed))

    def format_duration(self) -> str:
        seconds = self.remaining_seconds
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes:02d}:{seconds:02d}"

    def to_string(self) -> str:
        return (
            f"{self.emoji} **{self.name}**\n"
            f"└  Played by <@{self.user_id}> · "
            f"`{self.format_duration()}`"
        )