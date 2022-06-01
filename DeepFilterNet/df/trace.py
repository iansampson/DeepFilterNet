import argparse
import os
import time
import warnings
from typing import Optional, Tuple, Union

import torch
import torchaudio as ta
from loguru import logger
from numpy import ndarray
from torch import Tensor, nn
from torch.nn import functional as F
from torchaudio.backend.common import AudioMetaData

from df import config
from df.checkpoint import load_model as load_model_cp
from df.logger import init_logger, warn_once
from df.model import ModelParams
from df.modules import get_device
from df.utils import as_complex, as_real, download_file, get_cache_dir, get_norm_alpha, resample
from libdf import DF, erb, erb_norm, unit_norm

import coremltools as ct

from coremltools.converters.mil.frontend.torch.torch_op_registry import
     _TORCH_OPS_REGISTRY, register_torch_op
from coremltools.converters.mil.frontend.torch.ops import _get_inputs
from coremltools.converters.mil import Builder as mb
import numpy as _np

def _transpose_NHWC_to_NCHW(x):
    return mb.transpose(x=x, perm=[0, 3, 1, 2])

@register_torch_op
# TODO: Integrate with unfold
def extract_image_patches(context, node):
    inputs = _get_inputs(context, node)

    x = inputs[0]
    sizes = inputs[1]
    strides = inputs[2]
    rates = inputs[3]
    padding = inputs[4]

    if x.dim != 4:
        raise ValueError("input for ExtractImagePatches should be a 4D tensor.")
    if not all([rate == 1 for rate in rates]):
        raise NotImplementedError(
            "only rates with all 1s is implemented for ExtractImagePatches."
        )
    if len(sizes) != 4 or sizes[0] != 1 or sizes[3] != 1:
        raise ValueError(
            "ExtractImagePatches only supports sizes (4D tensor) with 1s for batch and channel dimensions."
        )
    if len(sizes) != 4 or strides[0] != 1 or strides[3] != 1:
        raise ValueError(
            "ExtractImagePatches only supports strides (4D tensor) with 1s for batch and channel dimensions."
        )
    if not padding in ["VALID", "SAME"]:
        raise ValueError("non-supported padding for ExtractImagePatches.")
    h, w = x.shape[1], x.shape[2]

    # padding for SAME mode
    if padding == "SAME":
        delta_h = h % strides[1] if h % strides[1] != 0 else strides[1]
        delta_w = w % strides[2] if w % strides[2] != 0 else strides[2]
        last_h = h - delta_h + 1
        last_w = w - delta_w + 1
        pad_h = max(0, last_h + sizes[1] - 1 - h)
        pad_w = max(0, last_w + sizes[2] - 1 - w)
        pad_h = [pad_h // 2, pad_h // 2 if pad_h % 2 == 0 else pad_h // 2 + 1]
        pad_w = [pad_w // 2, pad_w // 2 if pad_w % 2 == 0 else pad_w // 2 + 1]
        pad = _np.array([[0, 0], pad_h, pad_w, [0, 0]]).astype(_np.int32)
        pad = pad.reshape(-1)
        if not all(pad == 0):
            x = mb.pad(x=x, pad=pad, mode="constant", constant_val=0.0)
            h, w = x.shape[1], x.shape[2]

    # compute boxes
    batch = x.shape[0]
    boxes = []
    h_index = list(range(0, h - sizes[1] + 1, strides[1]))
    w_index = list(range(0, w - sizes[2] + 1, strides[2]))
    for hi in h_index:
        for wi in w_index:
            boxes.append((hi, wi, hi + sizes[1] - 1, wi + sizes[2] - 1))

    boxes = _np.array(boxes)
    box_indices = _np.arange(batch)
    box_indices = _np.tile(box_indices, (len(boxes), 1))
    box_indices = _np.transpose(box_indices)
    box_indices = box_indices.reshape(-1, 1)
    boxes = _np.tile(boxes, (batch, 1))
    boxes = _np.concatenate([box_indices, boxes], axis=1)
    boxes = boxes.reshape(boxes.shape[0], 1, boxes.shape[1], 1, 1)

    # use crop_and_resize
    x = _transpose_NHWC_to_NCHW(x)
    x = mb.crop_resize(
        x=x,
        roi=boxes,
        target_height=sizes[1],
        target_width=sizes[2],
        normalized_coordinates=False,
        spatial_scale=1.0,
        box_coordinate_mode="CORNERS_HEIGHT_FIRST",
        sampling_mode="ALIGN_CORNERS",
    )
    x = mb.squeeze(x=x, axes=[1])
    x = _transpose_NCHW_to_NHWC(x, node_name=node.name + "_transpose_to_nhwc")
    x = mb.reshape(x=x, shape=(batch, len(h_index), len(w_index), -1), name=node.name)
    context.add(node.name, x)

def main(args):
    model, df_state, suffix = init_df(
        args.model_base_dir,
        post_filter=args.pf,
        log_level=args.log_level,
        config_allow_defaults=True,
        epoch=args.epoch,
    )
    if args.output_dir is None:
        args.output_dir = "."
    elif not os.path.isdir(args.output_dir):
        os.mkdir(args.output_dir)
    df_sr = ModelParams().sr
    n_samples = len(args.noisy_audio_files)
    for i, file in enumerate(args.noisy_audio_files):
        progress = (i + 1) / n_samples * 100
        audio, meta = load_audio(file, df_sr)
        t0 = time.time()
        audio = enhance(
            model, df_state, audio, pad=args.compensate_delay, atten_lim_db=args.atten_lim
        )
        t1 = time.time()
        t_audio = audio.shape[-1] / df_sr
        t = t1 - t0
        rtf = t / t_audio
        fn = os.path.basename(file)
        p_str = f"{progress:2.0f}% | " if n_samples > 1 else ""
        logger.info(f"{p_str}Enhanced noisy audio file '{fn}' in {t:.1f}s (RT factor: {rtf:.3f})")
        audio = resample(audio, df_sr, meta.sample_rate)
        save_audio(
            file, audio, sr=meta.sample_rate, output_dir=args.output_dir, suffix=suffix, log=False
        )


def init_df(
    model_base_dir: Optional[str] = None,
    post_filter: bool = False,
    log_level: str = "INFO",
    log_file: Optional[str] = "enhance.log",
    config_allow_defaults: bool = False,
    epoch: Union[str, int, None] = "best",
    default_model: str = "DeepFilterNet2",
) -> Tuple[nn.Module, DF, str]:
    """Initializes and loads config, model and deep filtering state.

    Args:
        model_base_dir (str): Path to the model directory containing checkpoint and config. If None,
            load the pretrained DeepFilterNet2 model.
        post_filter (bool): Enable post filter for some minor, extra noise reduction.
        log_level (str): Control amount of logging. Defaults to `INFO`.
        log_file (str): Optional log file name. None disables it. Defaults to `enhance.log`.
        config_allow_defaults (bool): Whether to allow initializing new config values with defaults.
        epoch (str): Checkpoint epoch to load. Options are `best`, `latest`, `<int>`, and `none`.
            `none` disables checkpoint loading. Defaults to `best`.

    Returns:
        model (nn.Modules): Intialized model, moved to GPU if available.
        df_state (DF): Deep filtering state for stft/istft/erb
        suffix (str): Suffix based on the model name. This can be used for saving the enhanced
            audio.
    """
    try:
        from icecream import ic, install

        ic.configureOutput(includeContext=True)
        install()
    except ImportError:
        pass
    use_default_model = False
    if model_base_dir == "DeepFilterNet":
        default_model = "DeepFilterNet"
        use_default_model = True
    elif model_base_dir == "DeepFilterNet2":
        use_default_model = True
    if model_base_dir is None or use_default_model:
        use_default_model = True
        model_base_dir = ic(maybe_download_model(default_model))

    if not os.path.isdir(model_base_dir):
        raise NotADirectoryError("Base directory not found at {}".format(model_base_dir))
    log_file = os.path.join(model_base_dir, log_file) if log_file is not None else None
    init_logger(file=log_file, level=log_level, model=model_base_dir)
    if use_default_model:
        logger.info(f"Using {default_model} model at {model_base_dir}")
    config.load(
        os.path.join(model_base_dir, "config.ini"),
        config_must_exist=True,
        allow_defaults=config_allow_defaults,
        allow_reload=True,
    )
    if post_filter:
        config.set("mask_pf", True, bool, ModelParams().section)
        logger.info("Running with post-filter")
    p = ModelParams()
    df_state = DF(
        sr=p.sr,
        fft_size=p.fft_size,
        hop_size=p.hop_size,
        nb_bands=p.nb_erb,
        min_nb_erb_freqs=p.min_nb_freqs,
    )
    checkpoint_dir = os.path.join(model_base_dir, "checkpoints")
    load_cp = epoch is not None and not (isinstance(epoch, str) and epoch.lower() == "none")
    if not load_cp:
        checkpoint_dir = None
    try:
        mask_only = config.get("mask_only", cast=bool, section="train")
    except KeyError:
        mask_only = False
    model, epoch = load_model_cp(checkpoint_dir, df_state, epoch=epoch, mask_only=mask_only)
    if (epoch is None or epoch == 0) and load_cp:
        logger.error("Could not find a checkpoint")
        exit(1)
    logger.debug(f"Loaded checkpoint from epoch {epoch}")
    model = model.to(get_device())
    # Set suffix to model name
    suffix = os.path.basename(os.path.abspath(model_base_dir))
    if post_filter:
        suffix += "_pf"
    logger.info("Model loaded")
    return model, df_state, suffix


def df_features(audio: Tensor, df: DF, nb_df: int, device=None) -> Tuple[Tensor, Tensor, Tensor]:
    spec = df.analysis(audio.numpy())  # [C, Tf] -> [C, Tf, F]
    a = get_norm_alpha(False)
    erb_fb = df.erb_widths()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        erb_feat = torch.as_tensor(erb_norm(erb(spec, erb_fb), a)).unsqueeze(1)
    spec_feat = as_real(torch.as_tensor(unit_norm(spec[..., :nb_df], a)).unsqueeze(1))
    spec = as_real(torch.as_tensor(spec).unsqueeze(1))
    if device is not None:
        spec = spec.to(device)
        erb_feat = erb_feat.to(device)
        spec_feat = spec_feat.to(device)
    return spec, erb_feat, spec_feat


def load_audio(
    file: str, sr: Optional[int], verbose=True, **kwargs
) -> Tuple[Tensor, AudioMetaData]:
    """Loads an audio file using torchaudio.

    Args:
        file (str): Path to an audio file.
        sr (int): Optionally resample audio to specified target sampling rate.
        **kwargs: Passed to torchaudio.load(). Depends on the backend. The resample method
            may be set via `method` which is passed to `resample()`.

    Returns:
        audio (Tensor): Audio tensor of shape [C, T], if channels_first=True (default).
        info (AudioMetaData): Meta data of the original audio file. Contains the original sr.
    """
    ikwargs = {}
    if "format" in kwargs:
        ikwargs["format"] = kwargs["format"]
    rkwargs = {}
    if "method" in kwargs:
        rkwargs["method"] = kwargs.pop("method")
    info: AudioMetaData = ta.info(file, **ikwargs)
    audio, orig_sr = ta.load(file, **kwargs)
    if sr is not None and orig_sr != sr:
        if verbose:
            warn_once(
                f"Audio sampling rate does not match model sampling rate ({orig_sr}, {sr}). "
                "Resampling..."
            )
        audio = resample(audio, orig_sr, sr, **rkwargs)
    return audio, info


def save_audio(
    file: str,
    audio: Union[Tensor, ndarray],
    sr: int,
    output_dir: Optional[str] = None,
    suffix: Optional[str] = None,
    log: bool = False,
    dtype=torch.int16,
):
    outpath = file
    if suffix is not None:
        file, ext = os.path.splitext(file)
        outpath = file + f"_{suffix}" + ext
    if output_dir is not None:
        outpath = os.path.join(output_dir, os.path.basename(outpath))
    if log:
        logger.info(f"Saving audio file '{outpath}'")
    audio = torch.as_tensor(audio)
    if audio.ndim == 1:
        audio.unsqueeze_(0)
    if dtype == torch.int16 and audio.dtype != torch.int16:
        audio = (audio * (1 << 15)).to(torch.int16)
    if dtype == torch.float32 and audio.dtype != torch.float32:
        audio = audio.to(torch.float32) / (1 << 15)
    ta.save(outpath, audio, sr)


def df_features(audio: Tensor, df: DF, nb_df: int, device=None) -> Tuple[Tensor, Tensor, Tensor]:
    spec = df.analysis(audio.numpy())  # [C, Tf] -> [C, Tf, F]
    a = get_norm_alpha(False)
    erb_fb = df.erb_widths()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        erb_feat = torch.as_tensor(erb_norm(erb(spec, erb_fb), a)).unsqueeze(1)
    spec_feat = as_real(torch.as_tensor(unit_norm(spec[..., :nb_df], a)).unsqueeze(1))
    spec = as_real(torch.as_tensor(spec).unsqueeze(1))
    if device is not None:
        spec = spec.to(device)
        erb_feat = erb_feat.to(device)
        spec_feat = spec_feat.to(device)
    return spec, erb_feat, spec_feat


@torch.no_grad()
def enhance(
    model: nn.Module, df_state: DF, audio: Tensor, pad=False, atten_lim_db: Optional[float] = None
):
    model.eval()
    bs = audio.shape[0]
    if hasattr(model, "reset_h0"):
        model.reset_h0(batch_size=bs, device=get_device())
    orig_len = audio.shape[-1]
    n_fft, hop = 0, 0
    if pad:
        n_fft, hop = df_state.fft_size(), df_state.hop_size()
        # Pad audio to compensate for the delay due to the real-time STFT implementation
        audio = F.pad(audio, (0, n_fft))
    nb_df = getattr(model, "nb_df", getattr(model, "df_bins", ModelParams().nb_df))
    spec, erb_feat, spec_feat = df_features(audio, df_state, nb_df, device=get_device())

    # Convert model to CoreML
    traced_model = torch.jit.trace(model, (spec, erb_feat, spec_feat))

    spec_shape = ct.Shape(shape=(1, 1, ct.RangeDim(), 481, 2))
    spec_tensor = ct.TensorType(shape=spec_shape)

    erb_shape = ct.Shape(shape=(1, 1, ct.RangeDim(), 32))
    erb_tensor = ct.TensorType(shape=erb_shape)

    spec_feat_shape = ct.Shape(shape=(1, 1, ct.RangeDim(), 96, 2))
    spec_feat_tensor = ct.TensorType(shape=spec_feat_shape)

    core_ml_model = ct.convert(traced_model,
                               inputs=[spec_tensor, erb_tensor, spec_feat_tensor],
                               convert_to="mlprogram")
    core_ml_model.save("DeepFilterNet.mlpackage")

    enhanced = model(spec, erb_feat, spec_feat)[0].cpu()
    enhanced = as_complex(enhanced.squeeze(1))
    if atten_lim_db is not None and abs(atten_lim_db) > 0:
        lim = 10 ** (-abs(atten_lim_db) / 20)
        enhanced = as_complex(spec.squeeze(1)) * lim + enhanced * (1 - lim)
    audio = torch.as_tensor(df_state.synthesis(enhanced.numpy()))
    if pad:
        # The frame size is equal to p.hop_size. Given a new frame, the STFT loop requires e.g.
        # ceil((n_fft-hop)/hop). I.e. for 50% overlap, then hop=n_fft//2
        # requires 1 additional frame lookahead; 75% requires 3 additional frames lookahead.
        # Thus, the STFT/ISTFT loop introduces an algorithmic delay of n_fft - hop.
        assert n_fft % hop == 0  # This is only tested for 50% and 75% overlap
        d = n_fft - hop
        audio = audio[:, d : orig_len + d]
    return audio


def maybe_download_model(name: str = "DeepFilterNet") -> str:
    """Download a DeepFilterNet model.

    Args:
        - name (str): Model name. Currently needs to one of `[DeepFilterNet, DeepFilterNet2]`.

    Returns:
        - base_dir: Return the model base directory as string.
    """
    cache_dir = get_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    if name.endswith(".zip"):
        name = name.removesuffix(".zip")
    url = f"https://github.com/Rikorose/DeepFilterNet/raw/main/models/{name}"
    download_file(url + ".zip", cache_dir, extract=True)
    return os.path.join(cache_dir, name)


def parse_epoch_type(value: str) -> Union[int, str]:
    try:
        return int(value)
    except ValueError:
        assert value in ("best", "latest")
        return value


def setup_df_argument_parser(default_log_level: str = "INFO") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-base-dir",
        "-m",
        type=str,
        default=None,
        help="Model directory containing checkpoints and config. "
        "To load a pretrained model, you may just provide the model name, e.g. `DeepFilterNet`. "
        "By default, the pretrained DeepFilterNet2 model is loaded.",
    )
    parser.add_argument(
        "--pf",
        help="Post-filter that slightly over-attenuates very noisy sections.",
        action="store_true",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=str,
        default=None,
        help="Directory in which the enhanced audio files will be stored.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=default_log_level,
        help="Logger verbosity. Can be one of (debug, info, error, none)",
    )
    parser.add_argument("--debug", "-d", action="store_const", const="DEBUG", dest="log_level")
    parser.add_argument(
        "--epoch",
        "-e",
        default="best",
        type=parse_epoch_type,
        help="Epoch for checkpoint loading. Can be one of ['best', 'latest', <int>].",
    )
    return parser


def run():
    parser = setup_df_argument_parser()
    parser.add_argument(
        "--compensate-delay",
        "-D",
        action="store_true",
        help="Add some paddig to compensate the delay introduced by the real-time STFT/ISTFT implementation.",
    )
    parser.add_argument(
        "--atten-lim",
        "-a",
        type=int,
        default=None,
        help="Attenuation limit in dB by mixing the enhanced signal with the noisy signal.",
    )
    parser.add_argument(
        "noisy_audio_files",
        type=str,
        nargs="+",
        help="List of noise files to mix with the clean speech file.",
    )
    main(parser.parse_args())


if __name__ == "__main__":
    run()
