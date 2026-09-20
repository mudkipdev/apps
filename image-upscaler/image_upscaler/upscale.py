import os
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .model import MODELS, Model

Progress = Callable[[float, str], None]

ENGINE_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
BINARY_NAME = "realesrgan-ncnn-vulkan"


class UpscaleCancelled(Exception):
    pass


def _download(url: str, destination: Path, progress: Progress, label: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.parent / f"{destination.name}.partial"
    request = urllib.request.Request(url, headers={"User-Agent": "image-upscaler"})
    with urllib.request.urlopen(request) as response, open(partial, "wb") as handle:
        total = int(response.headers.get("Content-Length", 0))
        received = 0
        while chunk := response.read(1 << 16):
            handle.write(chunk)
            received += len(chunk)
            progress(received / total if total else 0.0, f"Downloading {label}")
    partial.replace(destination)


class Upscaler:
    def __init__(self, data_dir: Path):
        self.models_dir = data_dir / "models"
        self.binary = data_dir / BINARY_NAME

    def engine_installed(self) -> bool:
        return self.binary.is_file()

    def model_installed(self, model: Model) -> bool:
        return (self.models_dir / model.param).is_file() and (self.models_dir / model.bin).is_file()

    def installed_models(self) -> list[Model]:
        return [model for model in MODELS if self.model_installed(model)]

    def install_engine(self, progress: Progress) -> None:
        if self.engine_installed():
            return
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(suffix=".zip")
        os.close(descriptor)
        archive = Path(name)
        try:
            _download(ENGINE_URL, archive, progress, "engine")
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.namelist():
                    if member == BINARY_NAME or member.startswith("models/"):
                        bundle.extract(member, self.binary.parent)
            self.binary.chmod(0o755)
        finally:
            archive.unlink(missing_ok=True)

    def install_model(self, model: Model, progress: Progress) -> None:
        assert model.url is not None
        _download(f"{model.url}.param", self.models_dir / model.param, progress, model.label)
        _download(f"{model.url}.bin", self.models_dir / model.bin, progress, model.label)

    def remove_model(self, model: Model) -> None:
        (self.models_dir / model.param).unlink(missing_ok=True)
        (self.models_dir / model.bin).unlink(missing_ok=True)

    def upscale(self, model: Model, source: Path, destination: Path, progress: Progress, cancelled: Callable[[], bool]) -> None:
        command = [
            str(self.binary), "-i", str(source), "-o", str(destination),
            "-n", model.id, "-s", str(model.scale), "-m", str(self.models_dir),
        ]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        assert process.stderr is not None
        recent: list[str] = []
        while True:
            line = process.stderr.readline().strip()
            if line:
                recent = (recent + [line])[-8:]
                if line.endswith("%"):
                    progress(min(float(line.rstrip("%")) / 100, 1.0), f"Upscaling with {model.label}")
            if cancelled():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise UpscaleCancelled()
            if not line and process.poll() is not None:
                break
        if process.returncode:
            raise RuntimeError(next((line for line in reversed(recent) if "fail" in line.lower() or "error" in line.lower()), "The upscaler could not process this image."))
        progress(1.0, "Upscale complete")
