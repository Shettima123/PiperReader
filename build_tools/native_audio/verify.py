"""Exercise the replacement DLL through SoundFile, without changing installed files."""
import argparse
import ctypes
import hashlib
import io
import json
from pathlib import Path
import sys
import types

parser = argparse.ArgumentParser()
parser.add_argument("dll", type=Path)
parser.add_argument("--report", type=Path)
parser.add_argument("--mp3-fixture", type=Path)
args = parser.parse_args()
dll = args.dll.resolve()
assert dll.name == "libsndfile_x64.dll", dll
data_package = types.ModuleType("_soundfile_data")
data_package.__file__ = str(dll.parent / "__init__.py")
sys.modules["_soundfile_data"] = data_package
import numpy as np
import soundfile as sf

assert Path(sf._full_path).resolve() == dll, sf._full_path
native = ctypes.CDLL(str(dll))
assert int(sf._ffi.cast("uintptr_t", sf._snd.sf_version_string)) == ctypes.cast(native.sf_version_string, ctypes.c_void_p).value
assert sf.__libsndfile_version__ == "1.2.2", sf.__libsndfile_version__
results = {"loaded_requested_dll": True, "library_version": sf.__libsndfile_version__,
           "dll_sha256": hashlib.sha256(dll.read_bytes()).hexdigest()}
for rate, channels in ((24000, 1), (44100, 2)):
    mono = (0.2 * np.sin(2 * np.pi * 440 * np.arange(rate) / rate)).astype("float32")
    signal = mono if channels == 1 else np.column_stack((mono, mono * 0.6))
    for subtype in ("PCM_16", "FLOAT"):
        buffer = io.BytesIO()
        sf.write(buffer, signal, rate, format="WAV", subtype=subtype)
        buffer.seek(0)
        decoded, decoded_rate = sf.read(buffer, dtype="float32")
        assert decoded_rate == rate and decoded.shape == signal.shape
        assert np.max(np.abs(decoded - signal)) < 0.00004
        results[f"wav_{rate}_{channels}_{subtype}"] = True
    buffer = io.BytesIO()
    sf.write(buffer, signal, rate, format="MP3", subtype="MPEG_LAYER_III")
    assert buffer.getbuffer().nbytes > 1000
    buffer.seek(0)
    decoded, decoded_rate = sf.read(buffer, dtype="float32")
    assert decoded_rate == rate and decoded.shape == signal.shape
    assert np.corrcoef(decoded.reshape(-1), signal.reshape(-1))[0, 1] > 0.98
    results[f"mp3_roundtrip_{rate}_{channels}"] = True
if args.mp3_fixture:
    samples, rate = sf.read(args.mp3_fixture, dtype="float32")
    assert rate >= 16000 and samples.size > rate // 2 and np.max(np.abs(samples)) > 0.01
    results["independently_encoded_mp3_decode"] = True
assert "FLAC" not in sf.available_formats()
assert "OGG" not in sf.available_formats()
results["unbundled_external_codecs_disabled"] = True
results["passed"] = True
if args.report:
    args.report.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
print(json.dumps(results, indent=2))
