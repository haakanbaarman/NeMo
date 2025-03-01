# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

from dataclasses import dataclass
from typing import List

import torch

from nemo.collections.asr.parts.utils import rnnt_utils
from nemo.core.classes import Typing, typecheck
from nemo.core.neural_types import HypothesisType, LengthsType, LogprobsType, NeuralType


def pack_hypotheses(hypotheses: List[rnnt_utils.Hypothesis], logitlen: torch.Tensor,) -> List[rnnt_utils.Hypothesis]:

    if logitlen is not None:
        if hasattr(logitlen, 'cpu'):
            logitlen_cpu = logitlen.to('cpu')
        else:
            logitlen_cpu = logitlen

    for idx, hyp in enumerate(hypotheses):  # type: rnnt_utils.Hypothesis
        hyp.y_sequence = torch.tensor(hyp.y_sequence, dtype=torch.long)

        if logitlen is not None:
            hyp.length = logitlen_cpu[idx]

        if hyp.dec_state is not None:
            hyp.dec_state = _states_to_device(hyp.dec_state)

    return hypotheses


def _states_to_device(dec_state, device='cpu'):
    if torch.is_tensor(dec_state):
        dec_state = dec_state.to(device)

    elif isinstance(dec_state, (list, tuple)):
        dec_state = tuple(_states_to_device(dec_i, device) for dec_i in dec_state)

    return dec_state


class GreedyCTCInfer(Typing):
    """A greedy CTC decoder.

    Provides a common abstraction for sample level and batch level greedy decoding.

    Args:
        blank_index: int index of the blank token. Can be 0 or len(vocabulary).
        preserve_alignments: Bool flag which preserves the history of logprobs generated during
            decoding (sample / batched). When set to true, the Hypothesis will contain
            the non-null value for `logprobs` in it. Here, `logprobs` is a torch.Tensors.
        compute_timestamps: A bool flag, which determines whether to compute the character/subword, or
                word based timestamp mapping the output log-probabilities to discrite intervals of timestamps.
                The timestamps will be available in the returned Hypothesis.timestep as a dictionary.

    """

    @property
    def input_types(self):
        """Returns definitions of module input ports.
        """
        # Input can be of dimention -
        # ('B', 'T', 'D') [Log probs] or ('B', 'T') [Labels]

        return {
            "decoder_output": NeuralType(None, LogprobsType()),
            "decoder_lengths": NeuralType(tuple('B'), LengthsType()),
        }

    @property
    def output_types(self):
        """Returns definitions of module output ports.
        """
        return {"predictions": [NeuralType(elements_type=HypothesisType())]}

    def __init__(
        self, blank_id: int, preserve_alignments: bool = False, compute_timestamps: bool = False,
    ):
        super().__init__()

        self.blank_id = blank_id
        self.preserve_alignments = preserve_alignments
        self.compute_timestamps = compute_timestamps

    @typecheck()
    def forward(
        self, decoder_output: torch.Tensor, decoder_lengths: torch.Tensor,
    ):
        """Returns a list of hypotheses given an input batch of the encoder hidden embedding.
        Output token is generated auto-repressively.

        Args:
            decoder_output: A tensor of size (batch, timesteps, features) or (batch, timesteps) (each timestep is a label).
            decoder_lengths: list of int representing the length of each sequence
                output sequence.

        Returns:
            packed list containing batch number of sentences (Hypotheses).
        """
        with torch.inference_mode():
            hypotheses = []
            # Process each sequence independently
            prediction_cpu_tensor = decoder_output.cpu()

            if prediction_cpu_tensor.ndim < 2 or prediction_cpu_tensor.ndim > 3:
                raise ValueError(
                    f"`decoder_output` must be a tensor of shape [B, T] (labels, int) or "
                    f"[B, T, V] (log probs, float). Provided shape = {prediction_cpu_tensor.shape}"
                )

            # determine type of input - logprobs or labels
            if prediction_cpu_tensor.ndim == 2:  # labels
                greedy_decode = self._greedy_decode_labels
            else:
                greedy_decode = self._greedy_decode_logprobs

            for ind in range(prediction_cpu_tensor.shape[0]):
                out_len = decoder_lengths[ind] if decoder_lengths is not None else None
                hypothesis = greedy_decode(prediction_cpu_tensor[ind], out_len)
                hypotheses.append(hypothesis)

            # Pack results into Hypotheses
            packed_result = pack_hypotheses(hypotheses, decoder_lengths)

        return (packed_result,)

    @torch.no_grad()
    def _greedy_decode_logprobs(self, x: torch.Tensor, out_len: torch.Tensor):
        # x: [T, D]
        # out_len: [seq_len]

        # Initialize blank state and empty label set in Hypothesis
        hypothesis = rnnt_utils.Hypothesis(score=0.0, y_sequence=[], dec_state=None, timestep=[], last_token=None)
        prediction = x.detach().cpu()

        if out_len is not None:
            prediction = prediction[:out_len]

        prediction_logprobs, prediction_labels = prediction.max(dim=-1)

        non_blank_ids = prediction_labels != self.blank_id
        hypothesis.y_sequence = prediction_labels.numpy().tolist()
        hypothesis.score = (prediction_logprobs[non_blank_ids]).sum()

        if self.preserve_alignments:
            hypothesis.alignments = prediction.clone()

        if self.compute_timestamps:
            hypothesis.timestep = torch.nonzero(non_blank_ids, as_tuple=False)[:, 0].numpy().tolist()

        return hypothesis

    @torch.no_grad()
    def _greedy_decode_labels(self, x: torch.Tensor, out_len: torch.Tensor):
        # x: [T]
        # out_len: [seq_len]

        # Initialize blank state and empty label set in Hypothesis
        hypothesis = rnnt_utils.Hypothesis(score=0.0, y_sequence=[], dec_state=None, timestep=[], last_token=None)
        prediction_labels = x.detach().cpu()

        if out_len is not None:
            prediction_labels = prediction_labels[:out_len]

        non_blank_ids = prediction_labels != self.blank_id
        hypothesis.y_sequence = prediction_labels.numpy().tolist()
        hypothesis.score = -1.0

        if self.preserve_alignments:
            raise ValueError("Requested for alignments, but predictions provided were labels, not log probabilities.")

        if self.compute_timestamps:
            hypothesis.timestep = torch.nonzero(non_blank_ids, as_tuple=False)[:, 0].numpy().tolist()

        return hypothesis

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)


@dataclass
class GreedyCTCInferConfig:
    preserve_alignments: bool = False
    compute_timestamps: bool = False
