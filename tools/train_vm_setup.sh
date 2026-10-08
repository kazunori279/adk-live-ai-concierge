#!/bin/bash
# Provision a GCE GPU VM to train a custom openWakeWord model.
#
# Follows openWakeWord's automatic_model_training.ipynb, minus the TensorFlow
# stack: that exists only to emit a .tflite, and the Pi runs the ONNX path
# because tflite-runtime has no wheel for its Python 3.13.
#
# Run on the VM, not the Mac. Safe to re-run; every step is skipped if its
# output already exists. It takes far longer than an ssh session lasts, so run
# it detached — and note that logind kills the whole user slice on logout, tmux
# server included, unless the account lingers:
#
#   sudo loginctl enable-linger "$USER"
#   tmux new-session -d -s setup 'bash ~/setup.sh > ~/setup.log 2>&1'
set -eux
cd "$HOME"

sudo apt-get -qq update
sudo apt-get -qq install -y espeak-ng ffmpeg

pip install -q --upgrade pip
# torch-audiomentations is 0.12, not the 0.11.0 the upstream notebook pins:
# 0.11.0 calls torchaudio.set_audio_backend(), gone since torchaudio 2.1.
# espeak-phonemizer is piper-sample-generator's, installed --no-deps so it
# cannot drag torch along with it.
pip install -q webrtcvad mutagen==1.47.0 torchinfo torchmetrics \
  audiomentations==0.33.0 torch-audiomentations==0.12.0 acoustics==0.2.6 \
  pronouncing==0.2.0 deep-phonemizer==0.0.19 \
  piper-phonemize onnx onnxscript speechbrain==0.5.14
pip install -q --no-deps espeak-phonemizer

# Same torch 2.6 weights_only breakage as the Piper checkpoint below, in
# deep-phonemizer this time: its released model pickles a Preprocessor object.
# Upstream is unmaintained, so patch the installed copy.
DP=$(python3 -c 'import dp.model.model as m; print(m.__file__)')
sed -i 's/torch\.load(checkpoint_path, map_location=device)/torch.load(checkpoint_path, map_location=device, weights_only=False)/' "$DP"

# Something above drags in the newest torchaudio, whose compiled extension is
# built against a newer libtorch than the image's: importing it dies with
# `undefined symbol: torch_library_impl`. Pin it back to the torch already here.
TORCH=$(python3 -c 'import torch; print(torch.__version__)')
pip install -q --no-deps --force-reinstall "torchaudio==$TORCH" \
  --index-url "https://download.pytorch.org/whl/${TORCH##*+}"

# torchaudio 2.9 dropped its own backends: `torchaudio.load` is now a thin
# wrapper over torchcodec, so augmentation dies without it. torchcodec pins
# nothing and its compiled extension segfaults on the wrong libtorch — 0.8.1 is
# the one that matches torch 2.9.1. Re-derive this if the image moves.
pip install -q --no-deps torchcodec==0.8.1

[ -d openWakeWord ] || git clone -q https://github.com/dscripka/openWakeWord.git
pip install -q -e ./openWakeWord

# torchaudio 2.9 also dropped `info`, which openWakeWord and torch-audiomentations
# both still call. Patching it into the shared torchaudio module from data.py
# fixes both: data.py is imported first, and the call sites run later.
python3 - <<'PY'
import pathlib
p = pathlib.Path("openWakeWord/openwakeword/data.py")
s = p.read_text()
if "_AudioInfo" not in s:
    anchor = "import torchaudio\n"
    assert anchor in s, "data.py no longer imports torchaudio"
    p.write_text(s.replace(anchor, anchor + '''
if not hasattr(torchaudio, "info"):
    import soundfile as _sf

    class _AudioInfo:
        def __init__(self, path):
            i = _sf.info(str(path))
            self.sample_rate = i.samplerate
            self.num_frames = i.frames
            self.num_channels = i.channels

    torchaudio.info = _AudioInfo
''', 1))
    print("patched torchaudio.info shim into data.py")
PY

# torch 2.9's torch.onnx.export defaults to the dynamo exporter, which imports
# onnxscript. Missing, that raises at the very last line of a 40-minute run,
# with the trained model only in memory. onnxscript is installed above, but pin
# the legacy TorchScript exporter anyway: it is what opset 13 and openWakeWord's
# own loader were written against. Save the weights before exporting too, so
# whatever breaks there next time costs a re-export and not a re-train — the
# state_dict rather than the module, since Net is local to Model.__init__ and
# does not pickle.
python3 - <<'PY'
import pathlib
p = pathlib.Path("openWakeWord/openwakeword/train.py")
s = p.read_text()
old = """        model_to_save = copy.deepcopy(model)
        torch.onnx.export(model_to_save.to("cpu"), torch.rand(self.input_shape)[None, ],
                          os.path.join(output_dir, model_name + ".onnx"), opset_version=13)"""
new = """        model_to_save = copy.deepcopy(model)
        torch.save(model_to_save.to("cpu").state_dict(),
                   os.path.join(output_dir, model_name + ".pt"))
        torch.onnx.export(model_to_save.to("cpu"), torch.rand(self.input_shape)[None, ],
                          os.path.join(output_dir, model_name + ".onnx"), opset_version=13,
                          dynamo=False)"""
if new not in s:
    assert old in s, "train.py export_model no longer matches"
    p.write_text(s.replace(old, new, 1))
    print("patched export_model in train.py")
PY

# auto_train logs three numbers, at the end, after every knob it turned. Print
# each validation step so the negative-weight schedule can be read rather than
# guessed at.
python3 - <<'PY'
import pathlib
p = pathlib.Path("openWakeWord/openwakeword/train.py")
s = p.read_text()
old = """                self.history["val_accuracy"].append(val_acc.detach().cpu().numpy())
                self.history["val_recall"].append(val_recall)
                self.history["val_n_fp"].append(val_fp.detach().cpu().numpy())"""
new = old + """
                logging.info(f"val step {step_ndx}: recall {float(val_recall):.3f} "
                             f"acc {float(val_acc):.3f} n_fp {float(val_fp):.0f} "
                             f"fp/hr {float(self.history['val_fp_per_hr'][-1]):.3f}")"""
if new not in s:
    assert old in s, "train.py validation block no longer matches"
    p.write_text(s.replace(old, new, 1))
    print("patched validation logging into train.py")
PY

# augment_clips defaults RIR to probability 0.5, and train.py never passes
# augmentation_probabilities at all, so half of every batch is trained bone dry.
# That is what kills this model across a room: the round-1 weights score 0.95 on
# a dry 50 cm clip and a median of 0.29 once it goes through the very impulse
# responses it was augmented with. Make the dict a config key.
#
# It has to apply to the negatives as well as the positives. Reverberate only
# one side and the cheapest way to separate the classes becomes "is there a
# room", which scores beautifully on the validation set and fires at everything.
python3 - <<'PY'
import pathlib
p = pathlib.Path("openWakeWord/openwakeword/train.py")
s = p.read_text()
old = "    # Do Data Augmentation\n    if args.augment_clips is True:"
new = """    # Do Data Augmentation
    aug_probs = {
        "SevenBandParametricEQ": 0.25, "TanhDistortion": 0.25, "PitchShift": 0.25,
        "BandStopFilter": 0.25, "AddColoredNoise": 0.25, "AddBackgroundNoise": 0.75,
        "Gain": 1.0, "RIR": 0.5,
    }
    aug_probs.update(config.get("augmentation_probabilities", {}))
    logging.info(f"augmentation probabilities: {aug_probs}")
    if args.augment_clips is True:"""
if new not in s:
    assert old in s, "train.py augmentation block no longer matches"
    s = s.replace(old, new, 1)
    n = s.count("RIR_paths=rir_paths)")
    assert n == 4, f"expected 4 augment_clips calls, found {n}"
    s = s.replace("RIR_paths=rir_paths)",
                  "RIR_paths=rir_paths,\n" + " " * 58 + "augmentation_probabilities=aug_probs)")
    p.write_text(s)
    print("patched augmentation_probabilities into train.py")
PY

# The wheel ships no model weights; train.py needs the frozen feature extractor.
python3 -c "import openwakeword.utils as u; u.download_models()"

# dscripka's fork, not rhasspy's: train.py does `from generate_samples import
# generate_samples`, and only the fork has that module at the repo root.
if [ ! -d piper-sample-generator ]; then
  git clone -q https://github.com/dscripka/piper-sample-generator
  # v1.0.0's en-us-libritts-high, not v2.0.0's libritts_r-medium: the fork
  # hardcodes the former as its default and ships only that model's .json.
  wget -q --show-progress -O piper-sample-generator/models/en-us-libritts-high.pt \
    'https://github.com/rhasspy/piper-sample-generator/releases/download/v1.0.0/en-us-libritts-high.pt'
  # torch 2.6 flipped torch.load to weights_only=True, and this checkpoint
  # pickles a whole SynthesizerTrn object rather than a state dict. Unpickling
  # it runs code, but from the same release as generate_samples.py itself,
  # which this script already imports and runs — no new trust boundary.
  sed -i 's/torch\.load(model_path)/torch.load(model_path, weights_only=False)/' \
    piper-sample-generator/generate_samples.py
fi

# Room impulse responses: what makes the model survive being spoken to from
# across the room rather than into the HAT. Pulled as plain files rather than
# through `datasets`, whose streaming reader yields nothing for this repo.
# snapshot_download resumes, and copying over an existing file is cheap, so this
# runs unconditionally: a count-based "already done" guard cannot tell a finished
# copy from one that died half way, and the first attempt here died at 205 of 270.
python3 - <<'PY'
import os, shutil
from huggingface_hub import snapshot_download
out = "./mit_rirs"
os.makedirs(out, exist_ok=True)
src = snapshot_download("davidscripka/MIT_environmental_impulse_responses",
                        repo_type="dataset", allow_patterns="16khz/*.wav")
for f in os.scandir(os.path.join(src, "16khz")):
    shutil.copy(f.path, out)
print("RIRs:", len(os.listdir(out)))
PY

# Background noise: 8 AudioSet balanced-train shards, ~4000 clips of ~10 s.
#
# The upstream notebook wgets `data/bal_train00.tar`; that layout is gone and
# the repo now serves parquet, so the old URL 404s and takes the whole script
# down with it. Each shard is ~690 MB of flac wrapped in parquet, and all we
# want is 16 kHz mono, so stream the row groups over HTTP and decode in flight
# rather than landing 5.5 GB we would immediately throw away.
#
# The notebook also pulls the Free Music Archive for music, but that dataset now
# needs trust_remote_code=True — someone else's loader script running on this
# VM. AudioSet carries music of its own; take more shards instead.
mkdir -p audioset_16k .audioset_shards
python3 - <<'PY'
import io, os
import numpy as np, soundfile as sf, scipy.io.wavfile
from scipy.signal import resample_poly
import pyarrow.parquet as pq
from huggingface_hub import HfFileSystem
from tqdm import tqdm

out, marks = "./audioset_16k", "./.audioset_shards"
fs = HfFileSystem()
for shard in [f"{i:02d}" for i in range(8)]:
    mark = os.path.join(marks, shard)
    if os.path.exists(mark):
        continue
    path = f"datasets/agkphysics/AudioSet/data/bal_train/{shard}.parquet"
    pf = pq.ParquetFile(fs.open(path, "rb"))
    for batch in tqdm(pf.iter_batches(batch_size=50, columns=["video_id", "audio"]),
                      desc=shard, total=pf.metadata.num_rows // 50):
        for vid, a in zip(batch["video_id"].to_pylist(), batch["audio"].to_pylist()):
            x, sr = sf.read(io.BytesIO(a["bytes"]), dtype="float32", always_2d=True)
            x = x.mean(1)
            if sr != 16000:
                g = np.gcd(sr, 16000)
                x = resample_poly(x, 16000 // g, sr // g)
            scipy.io.wavfile.write(os.path.join(out, f"{vid}.wav"), 16000,
                                   np.clip(x * 32767, -32768, 32767).astype(np.int16))
    open(mark, "w").close()
print("background clips:", len(os.listdir(out)))
PY

# Pre-computed negatives: ~2000 h of speech embeddings, and an 11 h validation
# set that train.py uses to pick a threshold at a target false-positive rate.
#
# Downloaded to a .part and renamed on success: `wget -O dest` creates dest
# before it has a single byte, so an interrupted download leaves an empty file
# that every "already have it" check happily believes.
fetch() {  # fetch <url> <dest>
  [ -s "$2" ] && return 0
  wget -q --show-progress -O "$2.part" "$1"
  mv "$2.part" "$2"
}
B=https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main
fetch "$B/openwakeword_features_ACAV100M_2000_hrs_16bit.npy" \
  openwakeword_features_ACAV100M_2000_hrs_16bit.npy
fetch "$B/validation_set_features.npy" validation_set_features.npy

df -h "$HOME" | tail -1
echo SETUP-DONE
