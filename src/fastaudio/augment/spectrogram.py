import librosa
import torch
from fastai.imports import partial, random
from fastcore.transform import Transform
from fastcore.utils import ifnone, store_attr
from torch.nn import functional as F

from ..core.all import AudioSpectrogram
from .signal import AudioPadType


class CropTime(Transform):
    """Random crops full spectrogram to be length specified in ms by crop_duration"""

    def __init__(self, duration, pad_mode=AudioPadType.Zeros):
        store_attr(self, "duration, pad_mode")

    def encodes(self, sg: AudioSpectrogram) -> AudioSpectrogram:
        sr, hop = sg.sr, sg.hop_length
        w_crop = int((sr * self.duration) / (1000 * hop)) + 1
        w_sg = sg.shape[-1]
        if w_sg == w_crop:
            sg_crop = sg
        elif w_sg < w_crop:
            sg_crop = _tfm_pad_spectro(sg, w_crop, pad_mode=self.pad_mode)
        else:
            crop_start = random.randint(0, int(w_sg - w_crop))
            sg_crop = sg[:, :, crop_start : crop_start + w_crop]
            sg_crop.sample_start = int(crop_start * hop)
            sg_crop.sample_end = sg_crop.sample_start + int(self.duration * sr)
        sg.data = sg_crop
        return sg


def _tfm_pad_spectro(sg, width, pad_mode=AudioPadType.Zeros):
    """Pad spectrogram to specified width, using specified pad mode"""
    c, y, x = sg.shape
    pad_m = pad_mode.lower()
    if pad_m in ["zeros", "zeros_after"]:
        padded = torch.zeros((c, y, width))
        start = random.randint(0, width - x) if pad_m == "zeros" else 0
        padded[:, :, start : start + x] = sg.data
        return padded
    elif pad_m == "repeat":
        repeats = width // x + 1
        return sg.repeat(1, 1, repeats)[:, :, :width]
    else:
        raise ValueError(
            f"pad_mode {pad_m} not currently supported, only 'zeros', 'zeros_after', or 'repeat'"
        )


class MaskFreq(Transform):
    """Google SpecAugment frequency masking from https://arxiv.org/abs/1904.08779."""

    def __init__(self, num_masks=1, size=20, start=None, val=None, **kwargs):
        store_attr(self, "num_masks,size,start,val")

    def encodes(self, sg: AudioSpectrogram) -> AudioSpectrogram:
        channel_mean = sg.contiguous().view(sg.size(0), -1).mean(-1)[:, None, None]
        mask_val = ifnone(self.val, channel_mean)
        c, y, x = sg.shape
        # Position of the first mask
        start = ifnone(self.start, random.randint(0, y - self.size))
        for _ in range(self.num_masks):
            mask = torch.ones(self.size, x) * mask_val
            if not 0 <= start <= y - self.size:
                raise ValueError(
                    f"Start value '{start}' out of range for AudioSpectrogram of shape {sg.shape}"
                )
            sg[:, start : start + self.size, :] = mask
            # Setting start position for next mask
            start = random.randint(0, y - self.size)
        return sg


class MaskTime(Transform):
    """Google SpecAugment time masking from https://arxiv.org/abs/1904.08779."""

    def __init__(self, num_masks=1, size=20, start=None, val=None, **kwargs):
        store_attr(self, "num_masks,size,start,val,kwargs")

    def encodes(self, sg: AudioSpectrogram) -> AudioSpectrogram:
        sg.data = torch.einsum("...ij->...ji", sg)
        sg.data = MaskFreq(
            self.num_masks, self.size, self.start, self.val, **self.kwargs
        )(sg)
        sg.data = torch.einsum("...ij->...ji", sg)
        return sg


class SGRoll(Transform):
    """Shifts spectrogram along x-axis wrapping around to other side"""

    def __init__(self, max_shift_pct=0.5, direction=0, **kwargs):
        if int(direction) not in [-1, 0, 1]:
            raise ValueError("Direction must be -1(left) 0(bidirectional) or 1(right)")
        store_attr(self, "max_shift_pct,direction")

    def encodes(self, sg: AudioSpectrogram) -> AudioSpectrogram:
        direction = random.choice([-1, 1]) if self.direction == 0 else self.direction
        w = sg.shape[-1]
        roll_by = int(w * random.random() * self.max_shift_pct * direction)
        sg.data = sg.roll(roll_by, dims=-1)
        return sg


def _torchdelta(sg: AudioSpectrogram, order=1, width=9):
    """Converts to numpy, takes delta and converts back to torch, needs torchification"""
    if sg.shape[1] < width:
        raise ValueError(
            f"""Delta not possible with current settings, inputs must be wider than
        {width} columns, try setting max_to_pad to a larger value to ensure a minimum width"""
        )
    return AudioSpectrogram(
        torch.from_numpy(librosa.feature.delta(sg.numpy(), order=order, width=width))
    )


class Delta(Transform):
    """Creates delta with order 1 and 2 from spectrogram
    and concatenate with the original"""

    def __init__(self, width=9):
        self.td = partial(_torchdelta, width=width)

    def encodes(self, sg: AudioSpectrogram) -> AudioSpectrogram:
        new_channels = [
            torch.stack([c, self.td(c, order=1), self.td(c, order=2)]) for c in sg
        ]
        sg.data = torch.cat(new_channels, dim=0)
        return sg


class TfmResize(Transform):
    """Temporary fix to allow image resizing transform"""

    def __init__(self, size, interp_mode="bilinear", **kwargs):
        store_attr(self, "size,interp_mode")

    def encodes(self, sg: AudioSpectrogram) -> AudioSpectrogram:
        if isinstance(self.size, int):
            self.size = (self.size, self.size)
        c, y, x = sg.shape
        sg.data = F.interpolate(
            sg.unsqueeze(0), size=self.size, mode=self.interp_mode, align_corners=False
        ).squeeze(0)
        return sg