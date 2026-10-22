import array
import queue
import subprocess
import threading

import discord


class SoundMixer(discord.AudioSource):
    FRAME_BYTES = 3840

    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg_path = ffmpeg_path
        self.lock = threading.Lock()
        self.sources = []
        self.closed = False

    def add_sound(self, path, on_finished=None):
        if self.closed:
            raise RuntimeError("Mixer is closed")

        process = subprocess.Popen(
            [
                self.ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel", "error",
                "-i", str(path),
                "-f", "s16le",
                "-ar", "48000",
                "-ac", "2",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

        source = {
            "process": process,
            "frames": queue.Queue(maxsize=10),
            "on_finished": on_finished,
        }

        with self.lock:
            if self.closed:
                process.kill()
                process.wait()
                raise RuntimeError("Mixer is closed")
            self.sources.append(source)

        def read_audio():
            frames = source["frames"]
            try:
                while not self.closed:
                    chunk = process.stdout.read(self.FRAME_BYTES)
                    if not chunk:
                        break

                    chunk = chunk.ljust(self.FRAME_BYTES, b"\0")

                    while not self.closed:
                        try:
                            frames.put(chunk, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            except (OSError, ValueError):
                pass
            finally:
                while not self.closed:
                    try:
                        frames.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        continue

        threading.Thread(target=read_audio, daemon=True).start()

    def read(self):
        if self.closed:
            return b""

        with self.lock:
            sources = list(self.sources)

        mixed = array.array("h", [0] * (self.FRAME_BYTES // 2))
        finished = []

        for source in sources:
            try:
                frame = source["frames"].get_nowait()
            except queue.Empty:
                continue

            if frame is None:
                finished.append(source)
                continue

            samples = array.array("h")
            samples.frombytes(frame)

            for i, sample in enumerate(samples):
                mixed[i] = max(-32768, min(32767, mixed[i] + sample))

        with self.lock:
            for source in finished:
                if source in self.sources:
                    self.sources.remove(source)
                    process = source["process"]

                    try:
                        process.stdout.close()
                    except (OSError, ValueError):
                        pass

                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

                    callback = source.get("on_finished")
                    if callback:
                        callback()

        return mixed.tobytes()

    def is_opus(self):
        return False

    def stop_all(self):
        with self.lock:
            sources = list(self.sources)
            self.sources.clear()

        for source in sources:
            try:
                source["process"].kill()
            except OSError:
                pass

    def cleanup(self):
        with self.lock:
            self.closed = True
            sources = list(self.sources)
            self.sources.clear()

        for source in sources:
            try:
                source["process"].kill()
            except OSError:
                pass