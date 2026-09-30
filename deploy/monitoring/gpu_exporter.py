#!/usr/bin/env python
"""GPU metrics for Prometheus from NVML (nvidia-ml-py), 127.0.0.1:9115.

Runs as blinc-gpu-exporter.service from the unified venv; read-only NVML
queries (what nvidia-smi does), no CUDA context. Metrics:

  blinc_gpu_memory_used_bytes{gpu,name}   blinc_gpu_memory_total_bytes{gpu,name}
  blinc_gpu_utilization_ratio{gpu,name}   blinc_gpu_temperature_celsius{gpu,name}
  blinc_gpu_power_watts{gpu,name}         blinc_gpu_processes{gpu,name}
  blinc_gpu_process_memory_bytes{gpu,pid,process}   (who holds the VRAM)

BLINC_METRICS_PORT overrides the port (src/common/blinc_metrics.DEFAULT_PORTS['gpu']).
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', 'src', 'common')))
import blinc_metrics as bm  # noqa: E402

try:
    import pynvml
except ImportError:  # pragma: no cover
    pynvml = None
try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

_names = {}


def _text(v):
    return v.decode('utf-8', 'replace') if isinstance(v, bytes) else str(v)


def _devices():
    pynvml.nvmlInit()
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        yield str(i), _text(pynvml.nvmlDeviceGetName(h)), h


def _process_label(pid):
    if pid in _names:
        return _names[pid]
    label = str(pid)
    if psutil is not None:
        try:
            p = psutil.Process(pid)
            argv = p.cmdline()
            label = p.name()
            if label.startswith('python') and len(argv) > 1:
                label = '/'.join(argv[1].rstrip('/').split('/')[-2:])
        except Exception:
            pass
    _names[pid] = label
    return label


def _memory(kind):
    out = {}
    for gpu, name, h in _devices():
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        out[(gpu, name)] = mem.used if kind == 'used' else mem.total
    return out


def _per_device(fn):
    out = {}
    for gpu, name, h in _devices():
        try:
            out[(gpu, name)] = fn(h)
        except pynvml.NVMLError:
            continue
    return out


def _processes():
    out = {}
    for gpu, name, h in _devices():
        procs = []
        for getter in (pynvml.nvmlDeviceGetComputeRunningProcesses,
                       pynvml.nvmlDeviceGetGraphicsRunningProcesses):
            try:
                procs += getter(h)
            except pynvml.NVMLError:
                pass
        seen = set()
        for p in procs:
            if p.pid in seen or not p.usedGpuMemory:
                continue
            seen.add(p.pid)
            out[(gpu, str(p.pid), _process_label(p.pid))] = p.usedGpuMemory
    return out


def _process_count():
    counts = {}
    for (gpu, _pid, _label) in _processes():
        counts[gpu] = counts.get(gpu, 0) + 1
    return {(gpu, name): counts.get(gpu, 0) for gpu, name, _h in _devices()}


def register(registry=None):
    labels = ('gpu', 'name')
    bm.callback_gauge('gpu_memory_used_bytes', 'GPU memory in use', lambda: _memory('used'), labels, registry=registry)
    bm.callback_gauge('gpu_memory_total_bytes', 'GPU memory installed', lambda: _memory('total'), labels, registry=registry)
    bm.callback_gauge('gpu_utilization_ratio', 'GPU compute utilization (0-1)',
                      lambda: _per_device(lambda h: pynvml.nvmlDeviceGetUtilizationRates(h).gpu / 100.0), labels, registry=registry)
    bm.callback_gauge('gpu_temperature_celsius', 'GPU temperature',
                      lambda: _per_device(lambda h: pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)), labels, registry=registry)
    bm.callback_gauge('gpu_power_watts', 'GPU power draw',
                      lambda: _per_device(lambda h: pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0), labels, registry=registry)
    bm.callback_gauge('gpu_processes', 'Processes holding GPU memory', _process_count, labels, registry=registry)
    bm.callback_gauge('gpu_process_memory_bytes', 'GPU memory held per process',
                      _processes, ('gpu', 'pid', 'process'), registry=registry)


def main():
    if pynvml is None:
        sys.exit('nvidia-ml-py is not installed in this interpreter')
    bm.install_process_metrics('gpu_exporter')
    register()
    if not bm.start_exporter(bm.port_for('gpu')):
        sys.exit(1)
    while True:
        time.sleep(3600)


if __name__ == '__main__':
    main()
