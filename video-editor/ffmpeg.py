import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from model import MediaInfo, Operation, OperationKind


class ExportCancelled(Exception):
    pass


def dependencies_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path: Path) -> MediaInfo:
    process = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size:stream=codec_type,width,height", "-of", "json", str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    data = json.loads(process.stdout)
    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    file_format = data.get("format", {})
    return MediaInfo(
        path=path,
        duration=float(file_format.get("duration", 0)),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        size=int(file_format.get("size", path.stat().st_size)),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
    )


class Pipeline:
    def __init__(self, progress: Callable[[float, str], None], cancelled: Callable[[], bool]):
        self.progress = progress
        self.cancelled = cancelled

    def export(self, source: Path, destination: Path, operations: list[Operation]) -> None:
        if not dependencies_available():
            raise RuntimeError("FFmpeg and FFprobe must be installed to export video.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="video-editor-") as temporary:
            current = source
            count = len(operations) + 1
            for index, operation in enumerate(operations):
                self._check_cancelled()
                output = Path(temporary) / f"step-{index + 1}.mkv"
                label = operation_label(operation.kind)
                self.progress(index / count, f"Step {index + 1} of {len(operations)}: {label}")
                self._apply(current, output, operation)
                current = output

            self._check_cancelled()
            self.progress(len(operations) / count, "Writing export")
            descriptor, partial_name = tempfile.mkstemp(
                prefix=f".{destination.stem}-", suffix=".partial.mp4", dir=destination.parent,
            )
            os.close(descriptor)
            partial = Path(partial_name)
            try:
                if operations:
                    self._run(["ffmpeg", "-y", "-i", str(current), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", str(partial)])
                else:
                    self._run(self._encode_command(current, partial))
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
            self.progress(1.0, "Export complete")

    def _apply(self, source: Path, output: Path, operation: Operation) -> None:
        values = operation.values
        if operation.kind == OperationKind.TRIM:
            info = probe(source)
            start = float(values["start"])
            duration = info.duration - start - float(values["end"])
            if duration <= 0:
                raise ValueError("Trim removes the entire video.")
            self._run(self._encode_command(source, output, ["-ss", str(start), "-t", str(duration)]))
        elif operation.kind == OperationKind.CUT:
            self._cut(source, output, float(values["start"]), float(values["end"]))
        elif operation.kind == OperationKind.COMPRESS:
            self._compress(source, output, float(values["size_mib"]))
        elif operation.kind == OperationKind.AUDIO:
            audio = Path(str(values["path"]))
            if not audio.is_file():
                raise ValueError("Choose an audio file for the Add audio step.")
            self._audio(source, audio, output, str(values["mode"]))
        elif operation.kind == OperationKind.MUTE:
            self._run([
                "ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-c:v", "libx264",
                "-preset", "medium", "-crf", "20", "-an", str(output),
            ])
        elif operation.kind == OperationKind.RESIZE:
            height = int(values["height"])
            self._run(self._encode_command(source, output, ["-vf", f"scale=-2:{height}"]))

    def _encode_command(self, source: Path, output: Path, extra: list[str] | None = None) -> list[str]:
        return [
            "ffmpeg", "-y", "-i", str(source), *(extra or []), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(output),
        ]

    def _cut(self, source: Path, output: Path, start: float, end: float) -> None:
        info = probe(source)
        if start < 0 or end <= start or end >= info.duration:
            raise ValueError("A cut must have a valid start and end inside the video.")
        video_filter = (
            f"[0:v]trim=start=0:end={start},setpts=PTS-STARTPTS[v0];"
            f"[0:v]trim=start={end}:end={info.duration},setpts=PTS-STARTPTS[v1];"
            "[v0][v1]concat=n=2:v=1:a=0[v]"
        )
        command = ["ffmpeg", "-y", "-i", str(source)]
        if info.has_audio:
            filters = video_filter + (
                f";[0:a]atrim=start=0:end={start},asetpts=PTS-STARTPTS[a0];"
                f"[0:a]atrim=start={end}:end={info.duration},asetpts=PTS-STARTPTS[a1];"
                "[a0][a1]concat=n=2:v=0:a=1[a]"
            )
            command += ["-filter_complex", filters, "-map", "[v]", "-map", "[a]"]
        else:
            command += ["-filter_complex", video_filter, "-map", "[v]"]
        command += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(output)]
        self._run(command)

    def _audio(self, source: Path, audio: Path, output: Path, mode: str) -> None:
        info = probe(source)
        command = ["ffmpeg", "-y", "-i", str(source), "-stream_loop", "-1", "-i", str(audio), "-map", "0:v:0"]
        if mode == "mix" and info.has_audio:
            command += ["-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[a]", "-map", "[a]"]
        else:
            command += ["-map", "1:a:0"]
        command += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-shortest", str(output),
        ]
        self._run(command)

    def _compress(self, source: Path, output: Path, size_mib: float) -> None:
        info = probe(source)
        if size_mib <= 0 or info.duration <= 0:
            raise ValueError("Target size and video duration must be greater than zero.")
        audio_rate = 128_000 if info.has_audio else 0
        video_rate = int((size_mib * 1024 * 1024 * 8 * 0.96 / info.duration) - audio_rate)
        if video_rate < 100_000:
            raise ValueError("The target size is too small for this video.")
        passlog = output.with_suffix(".pass")
        first = [
            "ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-c:v", "libx264", "-b:v", str(video_rate),
            "-pass", "1", "-passlogfile", str(passlog), "-an", "-f", "null", os.devnull,
        ]
        second = [
            "ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
            "-b:v", str(video_rate), "-pass", "2", "-passlogfile", str(passlog),
            "-c:a", "aac", "-b:a", "128k", str(output),
        ]
        self._run(first)
        self._run(second)

    def _run(self, command: list[str]) -> None:
        self._check_cancelled()
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        assert process.stderr is not None
        recent: list[str] = []
        while True:
            line = process.stderr.readline()
            if line:
                recent.append(line.rstrip())
                recent = recent[-12:]
            if self.cancelled():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise ExportCancelled()
            if line == "" and process.poll() is not None:
                break
        if process.returncode:
            detail = next((line for line in reversed(recent) if "Error" in line or "Invalid" in line), "FFmpeg could not process the video.")
            raise RuntimeError(detail)

    def _check_cancelled(self) -> None:
        if self.cancelled():
            raise ExportCancelled()


def operation_label(kind: OperationKind) -> str:
    return {
        OperationKind.TRIM: "Trim start and end",
        OperationKind.CUT: "Cut out a region",
        OperationKind.COMPRESS: "Compress to a target size",
        OperationKind.AUDIO: "Add an audio track",
        OperationKind.MUTE: "Remove audio",
        OperationKind.RESIZE: "Resize video",
    }[kind]
