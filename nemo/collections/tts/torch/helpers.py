# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import functools
from pathlib import Path

import numpy as np
import torch
from scipy import ndimage
from scipy.stats import betabinom


class BetaBinomialInterpolator:
    """
        This module calculates alignment prior matrices (based on beta-binomial distribution) using cached popular sizes and image interpolation.
        The implementation is taken from https://github.com/NVIDIA/DeepLearningExamples/blob/master/PyTorch/SpeechSynthesis/FastPitch/fastpitch/data_function.py
    """

    def __init__(self, round_mel_len_to=50, round_text_len_to=10, cache_size=500):
        self.round_mel_len_to = round_mel_len_to
        self.round_text_len_to = round_text_len_to
        self.bank = functools.lru_cache(maxsize=cache_size)(beta_binomial_prior_distribution)

    @staticmethod
    def round(val, to):
        return max(1, int(np.round((val + 1) / to))) * to

    def __call__(self, w, h):
        bw = BetaBinomialInterpolator.round(w, to=self.round_mel_len_to)
        bh = BetaBinomialInterpolator.round(h, to=self.round_text_len_to)
        ret = ndimage.zoom(self.bank(bw, bh).T, zoom=(w / bw, h / bh), order=1)
        assert ret.shape[0] == w, ret.shape
        assert ret.shape[1] == h, ret.shape
        return ret


def general_padding(item, item_len, max_len, pad_value=0):
    if item_len < max_len:
        item = torch.nn.functional.pad(item, (0, max_len - item_len), value=pad_value)
    return item


def beta_binomial_prior_distribution(phoneme_count, mel_count, scaling_factor=1.0):
    x = np.arange(0, phoneme_count)
    mel_text_probs = []
    for i in range(1, mel_count + 1):
        a, b = scaling_factor * i, scaling_factor * (mel_count + 1 - i)
        mel_i_prob = betabinom(phoneme_count, a, b).pmf(x)
        mel_text_probs.append(mel_i_prob)
    return np.array(mel_text_probs)


def get_base_dir(paths):
    def is_relative_to(path1, path2):
        try:
            path1.relative_to(path2)
            return True
        except ValueError:
            return False

    def common_path(path1, path2):
        while path1 is not None:
            if is_relative_to(path2, path1):
                return path1
            path1 = path1.parent if path1 != path1.parent else None
        return None

    base_dir = None
    for p in paths:
        audio_dir = Path(p).parent
        if base_dir is None:
            base_dir = audio_dir
            continue
        base_dir = common_path(base_dir, audio_dir)

    return base_dir
