from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from uuid import uuid4


class OperationKind(str, Enum):
    TRIM = "trim"
    CUT = "cut"
    COMPRESS = "compress"
    AUDIO = "audio"
    MUTE = "mute"
    RESIZE = "resize"


@dataclass
class Operation:
    kind: OperationKind
    values: dict[str, object] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    @classmethod
    def with_defaults(cls, kind: OperationKind) -> "Operation":
        defaults: dict[OperationKind, dict[str, object]] = {
            OperationKind.TRIM: {"start": 0.0, "end": 0.0},
            OperationKind.CUT: {"start": 0.0, "end": 1.0},
            OperationKind.COMPRESS: {"size_mib": 25.0},
            OperationKind.AUDIO: {"path": "", "mode": "replace"},
            OperationKind.MUTE: {},
            OperationKind.RESIZE: {"height": 720},
        }
        return cls(kind, defaults[kind].copy())


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    size: int
    has_audio: bool

    @property
    def dimensions(self) -> str:
        return f"{self.width} × {self.height}" if self.width and self.height else "Unknown"

    @property
    def duration_label(self) -> str:
        total = max(0, round(self.duration))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"

    @property
    def size_label(self) -> str:
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"
