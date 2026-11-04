"""Optional prose summary of the digest from a small local language model.

The rule-based digest stays authoritative; prose is a few generated sentences on top,
marked as generated, and the dashboard works without it. ``LlamaCliProse`` runs
``llama-cli`` one-shot at the lowest CPU priority, and only in the ``normal`` governor mode.
The MVP ships it off (``NullProse``).
"""

from __future__ import annotations

import json
import subprocess
import typing
from pathlib import Path

from .config import ProseConfig

PROMPT = (
    "You summarize a radio scanner digest for a field station operator. Write 3 to 5 short"
    " plain sentences about what was busy, new, unusual or quiet, and the rig health. Use"
    " only facts in the JSON. Frequencies are in Hz; write them in MHz.\n\nJSON: {digest}\n\n"
    "Summary:"
)
MAX_CHARS = 600


class ProseEngine(typing.Protocol):
    def rewrite(self, digest: dict, mode: str = "normal") -> str | None: ...


class NullProse:
    def rewrite(self, digest: dict, mode: str = "normal") -> None:
        return None


class LlamaCliProse:
    def __init__(
        self,
        binary: Path,
        model: Path,
        threads: int = 2,
        run: typing.Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout_s: float = 600.0,
    ) -> None:
        self.binary = Path(binary)
        self.model = Path(model)
        self.threads = threads
        self._run = run
        self.timeout_s = timeout_s

    def argv(self, digest: dict) -> list[str]:
        compact = {k: digest[k] for k in sorted(digest) if k not in {"how", "prose", "labels"}}
        prompt = PROMPT.format(digest=json.dumps(compact, sort_keys=True, separators=(",", ":")))
        return [
            "nice", "-n", "19", str(self.binary), "-m", str(self.model), "-t", str(self.threads),
            "-n", "300", "--temp", "0.2", "-no-cnv", "--no-display-prompt", "-p", prompt,
        ]  # fmt: skip

    def rewrite(self, digest: dict, mode: str = "normal") -> str | None:
        if mode != "normal":
            return None
        try:
            proc = self._run(self.argv(digest), capture_output=True, timeout=self.timeout_s)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        text = " ".join(proc.stdout.decode("utf-8", "replace").split())
        return text[:MAX_CHARS] or None


def make_prose(cfg: ProseConfig) -> ProseEngine:
    if cfg.engine == "llama_cli" and cfg.binary is not None and cfg.model is not None:
        return LlamaCliProse(cfg.binary, cfg.model, threads=cfg.threads)
    return NullProse()
