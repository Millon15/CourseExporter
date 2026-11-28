"""Pins of pipeline D2 (user ruling 2026-11-27): the models, their revisions and the producer config string.

Constants only, no heavy imports: the VPS imports this module to fence a lease against the Mac's result.
"""

from __future__ import annotations

import hashlib

NEMOTRON = "nvidia/Nemotron-3-Diarization"
NEMOTRON_REV = "52a432672732425e4c19765b9fec9b5810e6f4ba"
NEMOTRON_FILES = ("config.json", "model.safetensors", "processor_config.json")
PARAKEET = "mlx-community/parakeet-tdt-0.6b-v3"
PARAKEET_REV = "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15"
PARAKEET_FILES = ("config.json", "model.safetensors")
DEVICE = "mps"
THRESHOLD = 0.5
SAMPLE_RATE = 16_000
ASR_CHUNK_S = 120.0
ASR_OVERLAP_S = 15.0
SEGMENT_MAX_S = 5.0
SEGMENT_PAUSE_S = 0.8

PRODUCER_CONFIG = (
    f"d2-nemotron3diar-{NEMOTRON_REV[:8]}-tf5241-{DEVICE}-fp32-offline340r40-t05-argmax"
    f"-parakeet-v3-{PARAKEET_REV[:8]}-mlx070-bf16-a120-o15-seg5-p08-v1"
)
CONFIG8 = hashlib.sha256(PRODUCER_CONFIG.encode()).hexdigest()[:8]
PRODUCER_NAME = "d2-nemotron3-parakeet3"
RESULT_VERSION = 1
SPEAKER_METHOD = "nemotron-3-diarization-exclusive"
TIMING_KIND = "segment"
