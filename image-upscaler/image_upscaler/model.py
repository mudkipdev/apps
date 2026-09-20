import json
from dataclasses import asdict, dataclass
from pathlib import Path

from gi.repository import GLib


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    scale: int
    stem: str
    url: str | None = None

    @property
    def param(self) -> str:
        return f"{self.stem}.param"

    @property
    def bin(self) -> str:
        return f"{self.stem}.bin"

    @property
    def bundled(self) -> bool:
        return self.url is None


UPSCAYL = "https://raw.githubusercontent.com/upscayl/upscayl/a00d55fee90e0f9435d5eaa86e76700df8199af8/resources/models"

MODELS: tuple[Model, ...] = (
    Model("realesrgan-x4plus", "Real-ESRGAN x4plus", 4, "realesrgan-x4plus"),
    Model("realesrgan-x4plus-anime", "Real-ESRGAN Anime", 4, "realesrgan-x4plus-anime"),
    Model("realesr-animevideov3", "AnimeVideo v3", 2, "realesr-animevideov3-x2"),
    Model("realesr-animevideov3", "AnimeVideo v3", 3, "realesr-animevideov3-x3"),
    Model("realesr-animevideov3", "AnimeVideo v3", 4, "realesr-animevideov3-x4"),
    Model("upscayl-lite-4x", "Upscayl Lite", 4, "upscayl-lite-4x", f"{UPSCAYL}/upscayl-lite-4x"),
    Model("digital-art-4x", "Digital Art", 4, "digital-art-4x", f"{UPSCAYL}/digital-art-4x"),
    Model("high-fidelity-4x", "High Fidelity", 4, "high-fidelity-4x", f"{UPSCAYL}/high-fidelity-4x"),
    Model("remacri-4x", "Remacri", 4, "remacri-4x", f"{UPSCAYL}/remacri-4x"),
    Model("ultramix-balanced-4x", "Ultramix Balanced", 4, "ultramix-balanced-4x", f"{UPSCAYL}/ultramix-balanced-4x"),
    Model("ultrasharp-4x", "Ultrasharp", 4, "ultrasharp-4x", f"{UPSCAYL}/ultrasharp-4x"),
    Model("upscayl-standard-4x", "Upscayl Standard", 4, "upscayl-standard-4x", f"{UPSCAYL}/upscayl-standard-4x"),
)

FORMATS = ("png", "jpg", "webp")


@dataclass
class Settings:
    model: str = ""
    format: str = "png"

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(config_path().read_text())
            return cls(model=str(data.get("model", "")), format=str(data.get("format", "png")))
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


def data_dir() -> Path:
    return Path(GLib.get_user_data_dir()) / "image-upscaler"


def config_path() -> Path:
    return Path(GLib.get_user_config_dir()) / "image-upscaler" / "settings.json"
