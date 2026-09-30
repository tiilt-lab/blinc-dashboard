"""Qwen3-ASR connector for post-hoc re-analysis.

Qwen3-ASR (Alibaba, Jan 2026; Apache-2.0) tops the open ASR leaderboards on
English WER. The qwen-asr package requires Python >=3.10, so transcription
runs in a dedicated qwen_venv via the qwen3_worker.py subprocess; this
connector parses its JSON and emits Google-shaped AsrResults, optionally
attaching pyannote speaker labels (same as the WhisperX path).

Measured on this instance: ~2.2x slower than real time for transcription +
alignment (a 5.5-min recording takes ~2 min), vs. seconds for WhisperX.
"""

import os
import logging

from .base_asr import (AsrResult, PosthocFileASR, worker_python,
                       run_json_worker, permissive_torch_load,
                       POSTHOC_WORKER_TIMEOUT)

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qwen3_worker.py")


class Qwen3ASR(PosthocFileASR):
    DRAIN_NAME = "qwen3"

    def __init__(self, audio_queue, transcript_queue, config, media_type,
                 interval, audio_file=None, model_id=None, diarize=False,
                 max_speakers=None, enrolled=None, speaker_model=None):
        self.audio_queue = audio_queue
        self.transcript_queue = transcript_queue
        self.config = config
        self.audio_file = audio_file
        self.model_id = model_id or "Qwen/Qwen3-ASR-1.7B"
        self.diarize = diarize
        # Optional constraint to a pod's known enrolled participants: cap
        # pyannote at max_speakers and remap clusters onto enrolled voices.
        self.max_speakers = max_speakers
        self.enrolled = enrolled
        self.speaker_model = speaker_model
        self.running = False

    # start()/stop()/_drain_queue() come from PosthocFileASR.

    def _speaker_turns(self):
        # pyannote diarization runs in THIS process — only qwen-asr needs 3.10.
        import torch
        pipeline = None
        try:
            with permissive_torch_load():
                from pyannote.audio import Pipeline
                token = os.environ.get("HUGGING_FACE_HUB_TOKEN")
                pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1", use_auth_token=token)
                pipeline.to(torch.device("cuda" if torch.cuda.is_available() else "cpu"))
                diarization = pipeline(self.audio_file, max_speakers=self.max_speakers)
                return [(turn.start, turn.end, label)
                        for turn, _, label in diarization.itertracks(yield_label=True)]
        finally:
            # The pipeline is rebuilt every run; without an explicit release the
            # CUDA allocator keeps its blocks reserved for the life of the server
            # process, so each run's ~2GB accumulates as idle VRAM.
            pipeline = None
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    @staticmethod
    def _speaker_at(turns, midpoint):
        for start, end, label in turns:
            if start <= midpoint <= end:
                return label
        return None

    def _transcribe_file(self):
        try:
            logging.info("Qwen3-ASR: transcribing %s via %s", self.audio_file, self.model_id)
            data = run_json_worker(
                lambda out_path: [worker_python("qwen_venv", 2), _WORKER,
                                  self.audio_file, self.model_id, out_path],
                timeout=POSTHOC_WORKER_TIMEOUT,
                handle=getattr(self, 'worker', None))  # killable on cancel
            segments = data.get("segments", [])
            logging.info("Qwen3-ASR: %d segments", len(segments))

            turns = []
            cluster_map = {}
            if self.diarize:
                logging.info("Qwen3-ASR: running pyannote diarization")
                turns = self._speaker_turns()
                if turns and self.enrolled and self.speaker_model:
                    from asr_connectors.cluster_reconcile import build_cluster_to_enrolled_map
                    cluster_map = build_cluster_to_enrolled_map(
                        self.audio_file, turns, self.enrolled, self.speaker_model)

            for segment in segments:
                words = [(w, s, e) for (w, s, e) in segment.get("words", [])]
                text = segment.get("text", "").strip()
                if not text or not words:
                    continue
                result = AsrResult(text, words)
                if turns:
                    midpoint = (words[0][1] + words[-1][2]) / 2.0
                    label = self._speaker_at(turns, midpoint)
                    result.speaker_tag = cluster_map.get(label, label)
                self.transcript_queue.put(result)
        except Exception as e:
            logging.error("Qwen3-ASR transcription failed: %s", e, exc_info=True)
            self.fail(e)  # consumer keeps the pod's previous results
        finally:
            self.running = False
            self.transcript_queue.put(None)
