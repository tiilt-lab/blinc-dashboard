"""Where the live ECAPA speaker encoder runs (audit B.4).

Loaded without ``run_opts`` it ran on CPU: ~30 encodes per ASR segment per
pod, on 8 cores shared with ffmpeg, the video models, post-hoc and a
co-tenant. On CUDA an encode is milliseconds and the weights are ~80 MB.
CPU is the fallback when CUDA is unavailable, when loading on it fails, or
when free VRAM is below ``MIN_FREE_VRAM_MIB`` (the GPU also hosts
CrisperWhisper, the video models and llama-server).

``choose_device`` is the pure policy; ``load_ecapa`` applies it. Nothing here
touches the GPU until ``load_ecapa`` is called with a real loader.
"""
import logging

MIN_FREE_VRAM_MIB = 1500


def choose_device(cuda_available, free_vram_mib, min_free_mib=MIN_FREE_VRAM_MIB):
    """('cuda'|'cpu', reason-for-cpu|None). Unknown free VRAM (None) does not
    block CUDA — the same policy as the CrisperWhisper spawn check."""
    if not cuda_available:
        return 'cpu', 'CUDA not available'
    if free_vram_mib is not None and free_vram_mib < min_free_mib:
        return 'cpu', 'only %d MiB of GPU memory free (need %d)' % (free_vram_mib, min_free_mib)
    return 'cuda', None


def _cuda_available():
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _free_vram_mib():
    # Read-only reuse of the ASR tree's nvidia-smi probe (None when unknown).
    try:
        from asr_connectors.crisperwhisper_asr import free_vram_mib
        return free_vram_mib()
    except Exception:
        return None


def load_ecapa(from_hparams, source, savedir, cuda_available=None, free_vram=None, log=logging):
    """Load ``from_hparams(source=, savedir=)`` on the chosen device and return
    ``(model, device)``. Any failure on CUDA (driver, OOM at init, ...) falls
    back to a CPU load so the service still starts.

    ``cuda_available`` (bool) and ``free_vram`` (callable -> MiB|None) are
    injectable for tests; by default torch and nvidia-smi are asked.
    """
    if cuda_available is None:
        cuda_available = _cuda_available()
    free = None
    if cuda_available:
        free = (free_vram or _free_vram_mib)()
    device, why = choose_device(cuda_available, free)
    if device == 'cuda':
        try:
            model = from_hparams(source=source, savedir=savedir, run_opts={"device": "cuda"})
            log.info('ECAPA speaker encoder on CUDA (%s MiB free)', free if free is not None else '?')
            return model, 'cuda'
        except Exception as e:
            why = 'CUDA load failed: %s' % e
    model = from_hparams(source=source, savedir=savedir)
    log.info('ECAPA speaker encoder on CPU (%s)', why)
    return model, 'cpu'
