"""WhisperX connector for post-hoc re-analysis.

WhisperX (open source: faster-whisper backend + wav2vec2 forced alignment)
transcribes the WHOLE stored recording in one batched pass and returns
word-level timestamps far more accurate than streaming windows — the right
shape for post-hoc work, where the full file is available up front.

Unlike the streaming connectors it does not consume PCM from the audio queue;
it reads the recording file directly (the post-hoc stream reader still fills
the AudioBuffer that downstream diarization/DoA extraction needs — this
connector just drains the queue so the reader never blocks). Speaker
attribution stays with the existing fingerprint matching downstream.
"""

import logging

from .base_asr import AsrResult, PosthocFileASR, permissive_torch_load


class WhisperXASR(PosthocFileASR):
    DRAIN_NAME = "whisperx"

    def __init__(self, audio_queue, transcript_queue, config, media_type,
                 interval, audio_file=None, model_size=None, diarize=False,
                 max_speakers=None, enrolled=None, speaker_model=None,
                 diarizer=None):
        self.audio_queue = audio_queue
        self.transcript_queue = transcript_queue
        self.config = config
        self.audio_file = audio_file
        self.model_size = model_size or "small"
        # Optional constraint to a pod's known enrolled participants: cap
        # pyannote at max_speakers and remap clusters onto enrolled voices.
        self.max_speakers = max_speakers
        self.enrolled = enrolled
        self.speaker_model = speaker_model
        # Optional speaker-diarization pass. 'pyannote' (open SOTA clustering;
        # gated weights need HUGGING_FACE_HUB_TOKEN) or 'sortformer' (NVIDIA
        # streaming Sortformer 4-spk, runs in its own NeMo venv). Segments get
        # cluster labels attached as .speaker_tag on each AsrResult. The bare
        # `diarize` bool is the legacy pyannote switch.
        self.diarizer = diarizer or ("pyannote" if diarize else None)
        self.diarize = self.diarizer is not None
        self.running = False

    # start()/stop()/_drain_queue() come from PosthocFileASR. WhisperX reads
    # the file directly and only drains the PCM queue so the reader can't block.

    def _transcribe_file(self):
        try:
            import torch
            with permissive_torch_load():
                # The RTX 8000 is shared with a second instance, so a whisperx
                # model load / transcribe occasionally loses the memory race
                # and dies with "CUDA out of memory" — a transient spike, not a
                # real failure. Retry once after releasing our cache and letting
                # the concurrent spike (often this same pod's video pass) clear.
                import time as _time
                result, cluster_map = None, {}
                for attempt in range(2):
                    try:
                        result, cluster_map = self._run_gpu_pipeline(torch)
                        break
                    except RuntimeError as e:
                        if attempt == 0 and "out of memory" in str(e).lower():
                            logging.warning("WhisperX: CUDA OOM on shared GPU; "
                                            "releasing cache and retrying in 45s")
                            _time.sleep(45)
                            continue
                        raise

            emitted = 0
            for segment in result["segments"]:
                text = (segment.get("text") or "").strip()
                if not text:
                    continue
                words = [
                    (w["word"], w["start"], w["end"])
                    for w in segment.get("words", [])
                    if "start" in w and "end" in w
                ]
                if not words:
                    # Alignment can skip tokens (numbers etc.); fall back to
                    # the segment span so downstream timing still works.
                    words = [(text, segment.get("start", 0.0), segment.get("end", 0.0))]
                asr_result = AsrResult(text, words)
                # pyannote cluster label (SPEAKER_00...), remapped to an enrolled
                # alias when reconciliation is on; consumed downstream when
                # fingerprint matching yields nothing.
                _sp = segment.get("speaker")
                asr_result.speaker_tag = cluster_map.get(_sp, _sp)
                self.transcript_queue.put(asr_result)
                emitted += 1
            logging.info("WhisperX: emitted %d transcript results", emitted)
        except Exception as e:
            logging.error("WhisperX transcription failed: %s", e, exc_info=True)
            self.fail(e)  # consumer keeps the pod's previous results
        finally:
            # The GPU models are freed inside _run_gpu_pipeline (per attempt);
            # this is a final safety release of any residual cache.
            try:
                import gc
                import torch
                try:
                    del result
                except NameError:
                    pass
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    logging.info("WhisperX: released CUDA cache (%.1f GiB still reserved)",
                                 torch.cuda.memory_reserved() / 2**30)
            except Exception:
                pass
            self.running = False
            self.transcript_queue.put(None)

    def _run_gpu_pipeline(self, torch):
        # Load + transcribe + align + (optional) diarize on the GPU. Frees its
        # own model/audio allocations before returning OR raising, so a retry
        # after a CUDA OOM starts from a clean allocator.
        import whisperx
        model = align_model = diarizer = audio = None
        try:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
            logging.info("WhisperX: loading model '%s' on %s", self.model_size, device)
            model = whisperx.load_model(self.model_size, device, compute_type=compute)
            audio = whisperx.load_audio(self.audio_file)
            result = model.transcribe(audio, batch_size=16)
            language = result.get("language", "en")
            logging.info("WhisperX: transcribed %d segments (lang=%s); aligning",
                         len(result["segments"]), language)
            align_model, metadata = whisperx.load_align_model(
                language_code=language, device=device)
            result = whisperx.align(result["segments"], align_model,
                                    metadata, audio, device)
            cluster_map = {}
            if self.diarizer == "sortformer":
                import pandas as pd
                from whisperx.diarize import assign_word_speakers
                from asr_connectors.sortformer_diar import run_sortformer
                logging.info("WhisperX: running Sortformer diarization (NeMo venv)")
                sf_turns = run_sortformer(self.audio_file)
                diarize_segments = pd.DataFrame(
                    [{"start": s, "end": e, "speaker": spk} for s, e, spk in sf_turns])
                if len(diarize_segments):
                    result = assign_word_speakers(diarize_segments, result)
                logging.info("WhisperX: Sortformer diarization attached")
                if self.enrolled and self.speaker_model and sf_turns:
                    from asr_connectors.cluster_reconcile import build_cluster_to_enrolled_map
                    cluster_map = build_cluster_to_enrolled_map(
                        self.audio_file, sf_turns, self.enrolled, self.speaker_model)
            elif self.diarize:
                import os
                from whisperx.diarize import DiarizationPipeline, assign_word_speakers
                token = os.environ.get("HUGGING_FACE_HUB_TOKEN")
                logging.info("WhisperX: running pyannote diarization")
                diarizer = DiarizationPipeline(model_name="pyannote/speaker-diarization-3.1", token=token, device=device)
                diarize_segments = diarizer(audio, max_speakers=self.max_speakers)
                result = assign_word_speakers(diarize_segments, result)
                logging.info("WhisperX: diarization attached")
                if self.enrolled and self.speaker_model:
                    from asr_connectors.cluster_reconcile import build_cluster_to_enrolled_map
                    turns = [(row['start'], row['end'], row['speaker'])
                             for _, row in diarize_segments.iterrows()]
                    cluster_map = build_cluster_to_enrolled_map(
                        self.audio_file, turns, self.enrolled, self.speaker_model)
            return result, cluster_map
        finally:
            # Free GPU models/audio so the next attempt (or pod) starts clean.
            del model, align_model, diarizer, audio
            try:
                import gc
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
