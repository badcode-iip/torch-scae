# coding=utf-8
# Copyright 2020 The Google Research Authors.
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

"""Capsule layer."""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from monty.collections import AttrDict
from torch.distributions import Bernoulli, LogisticNormal, Normal

from torch_scae import cv_ops, math_ops
from torch_scae import nn_ext
from torch_scae.general_utils import prod
from torch_scae.math_ops import l2_loss


class CapsuleLayer(nn.Module):
    """Implementation of a capsule layer."""

    # number of parameters needed to parametrize linear transformations.
    n_transform_params = 6  # P

    def __init__(self,
                 n_caps,
                 dim_feature,
                 n_votes,
                 dim_caps,
                 hidden_sizes=(128,),
                 caps_dropout_rate=0.0,
                 learn_vote_scale=False,
                 allow_deformations=True,
                 noise_type=None,
                 noise_scale=0.,
                 similarity_transform=True,
                 ):
        """Builds the module.

        Args:
          n_caps: int, number of capsules.
          dim_caps: int, number of capsule parameters
          hidden_sizes: int or sequence of ints, number of hidden units for an MLP
            which predicts capsule params from the input encoding.
          n_caps_dims: int, number of capsule coordinates.
          caps_dropout_rate: float in [0, 1].
          n_votes: int, number of votes generated by each capsule.
          learn_vote_scale: bool, learns input-dependent scale for each
            capsules' votes.
          allow_deformations: bool, allows input-dependent deformations of capsule-part
            relationships.
          noise_type: 'normal', 'logistic' or None; noise type injected into
            presence logits.
          noise_scale: float >= 0. scale parameters for the noise.
          similarity_transform: boolean; uses similarity transforms if True.
        """
        super().__init__()

        self.n_caps = n_caps  # O
        self.dim_feature = dim_feature  # F
        self.hidden_sizes = list(hidden_sizes)  # [H_i, ...]
        self.dim_caps = dim_caps  # D
        self.caps_dropout_rate = caps_dropout_rate
        self.n_votes = n_votes
        self.learn_vote_scale = learn_vote_scale
        self.allow_deformations = allow_deformations
        self.noise_type = noise_type
        self.noise_scale = noise_scale

        self.similarity_transform = similarity_transform

        self._build()

    def _build(self):
        # Use separate parameters to do predictions for different capsules.
        sizes = [self.dim_feature] + self.hidden_sizes + [self.dim_caps]
        self.mlps = nn.ModuleList([
            nn_ext.MLP(sizes=sizes)
            for _ in range(self.n_caps)
        ])

        self.output_shapes = (
            [self.n_votes, self.n_transform_params],  # OPR-dynamic
            [1, self.n_transform_params],  # OVR
            [1],  # per-object presence
            [self.n_votes],  # per-vote-presence
            [self.n_votes],  # per-vote scale
        )
        self.splits = [prod(i) for i in self.output_shapes]
        self.n_outputs = sum(self.splits)  # A

        # we don't use bias in the output layer in order to separate the static
        # and dynamic parts of the OP
        sizes = [self.dim_caps + 1] + self.hidden_sizes + [self.n_outputs]
        self.caps_mlps = nn.ModuleList([
            nn_ext.MLP(sizes=sizes, bias=False)
            for _ in range(self.n_caps)
        ])

        self.caps_bias_list = nn.ParameterList([
            nn.Parameter(torch.zeros(1, self.n_caps, *shape), requires_grad=True)
            for shape in self.output_shapes[1:]
        ])

        # constant object-part relationship matrices, OPR-static
        self.cpr_static = nn.Parameter(
            torch.zeros([1, self.n_caps, self.n_votes, self.n_transform_params]),
            requires_grad=True
        )

    def forward(self, feature, parent_transform=None, parent_presence=None):
        """Builds the module.

        Args:
          feature: Tensor of encodings of shape [B, O, F].
          parent_transform: Tuple of (matrix, vector).
          parent_presence: pass

        Returns:
          A bunch of stuff.
        """
        device = next(iter(self.parameters())).device

        batch_size = feature.shape[0]  # B

        # Predict capsule and additional params from the input encoding.
        # [B, O, D]

        caps_feature_list = feature.unbind(1)  # [(B, F)] * O
        caps_param_list = [self.mlps[i](caps_feature_list[i])
                           for i in range(self.n_caps)]  # [(B, D)] * O
        del caps_feature_list
        raw_caps_param = torch.stack(caps_param_list, 1)  # (B, O, D)
        del caps_param_list

        if self.caps_dropout_rate == 0.0:
            caps_exist = torch.ones(batch_size, self.n_caps, 1)  # (B, O, 1)
        else:
            pmf = Bernoulli(1. - self.caps_dropout_rate)
            caps_exist = pmf.sample((batch_size, self.n_caps, 1))  # (B, O, 1)
        caps_exist = caps_exist.to(device)

        caps_param = torch.cat([raw_caps_param, caps_exist], -1)  # (B, O, D+1)
        del caps_exist

        caps_eparam_list = caps_param.unbind(1)  # [(B, D+1)] * O
        all_param_list = [self.caps_mlps[i](caps_eparam_list[i])
                          for i in range(self.n_caps)]  # [(B, A)] * O
        del caps_eparam_list
        all_param = torch.stack(all_param_list, 1)  # (B, O, A)
        del all_param_list
        all_param_split_list = torch.split(all_param, self.splits, -1)
        result = [t.view(batch_size, self.n_caps, *s)
                  for (t, s) in zip(all_param_split_list, self.output_shapes)]
        del all_param
        del all_param_split_list

        # add up static and dynamic object part relationship
        cpr_dynamic = result[0]
        if not self.allow_deformations:
            cpr_dynamic = torch.zeros_like(cpr_dynamic, device=device)
        cpr_dynamic_reg_loss = l2_loss(cpr_dynamic) / batch_size
        cpr = self._make_transform(cpr_dynamic + self.cpr_static)
        del cpr_dynamic

        # add bias to all remaining outputs
        cvr, presence_logit_per_caps, presence_logit_per_vote, scale_per_vote = [
            t + bias
            for (t, bias) in zip(result[1:], self.caps_bias_list)
        ]
        del result

        # this is for hierarchical
        if parent_transform is None:
            cvr = self._make_transform(cvr)
        else:
            cvr = parent_transform

        cvr_per_vote = cvr.repeat(1, 1, self.n_votes, 1, 1)
        vote = torch.matmul(cvr_per_vote, cpr)  # PVR = OVR x OPR
        del cvr_per_vote, cpr

        if self.caps_dropout_rate > 0.0:
            presence_logit_per_caps = presence_logit_per_caps \
                                      + math_ops.log_safe(caps_exist)

        def add_noise(tensor):
            """Adds noise to tensors."""
            if self.noise_type == 'uniform':
                noise = (torch.rand(tensor.shape) - 0.5) * self.noise_scale
            elif self.noise_type == 'logistic':
                pdf = LogisticNormal(0., self.noise_scale)
                noise = pdf.sample(tensor.shape)
            elif not self.noise_type:
                noise = torch.tensor([0.0])
            else:
                raise ValueError(f'Invalid noise type: {self.noise_type}')
            return tensor + noise.to(device)

        presence_logit_per_caps = add_noise(presence_logit_per_caps)
        presence_logit_per_vote = add_noise(presence_logit_per_vote)

        if parent_presence is not None:
            presence_per_caps = parent_presence
        else:
            presence_per_caps = torch.sigmoid(presence_logit_per_caps)

        presence_per_vote = presence_per_caps * torch.sigmoid(presence_logit_per_vote)
        del presence_per_caps

        if self.learn_vote_scale:
            # for numerical stability
            scale_per_vote = F.softplus(scale_per_vote + .5) + 1e-2
        else:
            scale_per_vote = torch.ones_like(scale_per_vote, device=device)

        return AttrDict(
            vote=vote,
            scale=scale_per_vote,
            vote_presence=presence_per_vote,
            presence_logit_per_caps=presence_logit_per_caps,
            presence_logit_per_vote=presence_logit_per_vote,
            cpr_dynamic_reg_loss=cpr_dynamic_reg_loss,
            raw_caps_param=raw_caps_param,
            raw_caps_feature=feature,
        )

    def _make_transform(self, params):
        return cv_ops.geometric_transform(params, self.similarity_transform,
                                          nonlinear=True, as_matrix=True)


class CapsuleLikelihood(nn.Module):
    """Capsule voting mechanism."""

    def __init__(self, vote, scale, vote_presence_prob, dummy_vote):
        super().__init__()
        self.n_votes = 1
        self.n_caps = vote.shape[1]  # O
        self.vote = vote  # (B, O, M, P)
        self.scale = scale  # (B, O, M)
        self.vote_presence_prob = vote_presence_prob  # (B, O, M)
        self.dummy_vote = dummy_vote

    def _get_pdf(self, votes, scales):
        return Normal(votes, scales)

    def log_prob(self, x, presence=None):
        return self(x, presence).log_prob

    def explain(self, x, presence=None):
        return self(x, presence).winner

    def forward(self, x, presence=None):  # (B, M, P), (B, M)
        device = x.device

        batch_size, n_input_points, dim_in = x.shape  # B, M, P

        # since scale is a per-caps scalar and we have one vote per capsule
        vote_component_pdf = self._get_pdf(self.vote,
                                           self.scale.unsqueeze(-1))

        # expand input along caps dimensions
        expanded_x = x.unsqueeze(1)  # (B, 1, M, P)
        vote_log_prob_per_dim = vote_component_pdf.log_prob(expanded_x)  # (B, O, M, P)
        vote_log_prob = vote_log_prob_per_dim.sum(-1)  # (B, O, M)
        del vote_log_prob_per_dim
        del x
        del expanded_x

        # (B, 1, M)
        dummy_vote_log_prob = torch.zeros(
            batch_size, 1, n_input_points, device=device) - 2. * np.log(10.)

        # (B, O+1, M)
        vote_log_prob = torch.cat([vote_log_prob, dummy_vote_log_prob], 1)
        del dummy_vote_log_prob

        #
        dummy_logit = torch.zeros(batch_size, 1, 1, device=device) - 2. * np.log(10.)
        dummy_logit = dummy_logit.repeat(1, 1, n_input_points)  # (B, 1, M)

        mixing_logit = math_ops.log_safe(self.vote_presence_prob)  # (B, O, M)
        mixing_logit = torch.cat([mixing_logit, dummy_logit], 1)  # (B, O+1, M)
        mixing_log_prob = mixing_logit - mixing_logit.logsumexp(1, keepdim=True)  # (B, O+1, M)

        # (B, M)
        mixture_log_prob_per_point = (mixing_logit + vote_log_prob).logsumexp(1)

        if presence is not None:
            presence = presence.float()
            mixture_log_prob_per_point = mixture_log_prob_per_point * presence

        # (B,)
        mixture_log_prob_per_example = mixture_log_prob_per_point.sum(1)
        del mixture_log_prob_per_point

        # scalar
        mixture_log_prob_per_batch = mixture_log_prob_per_example.mean()
        del mixture_log_prob_per_example

        # (B, O + 1, M)
        posterior_mixing_logits_per_point = mixing_logit + vote_log_prob
        del vote_log_prob

        # [B, M]
        winning_vote_idx = torch.argmax(
            posterior_mixing_logits_per_point[:, :-1], 1)

        batch_idx = torch.arange(batch_size, device=device).unsqueeze(1)  # (B, 1)
        batch_idx = batch_idx.repeat(1, n_input_points)  # (B, M)

        point_idx = torch.arange(n_input_points, device=device).unsqueeze(0)  # (1, M)
        point_idx = point_idx.repeat(batch_size, 1)  # (B, M)

        idx = torch.stack([batch_idx, winning_vote_idx, point_idx], -1)
        del batch_idx
        del point_idx

        # (B, M, P)
        winning_vote = self.vote[idx[:, :, 0], idx[:, :, 1], idx[:, :, 2]]
        assert winning_vote.shape == (batch_size, n_input_points, dim_in)

        # (B, M)
        winning_presence = \
            self.vote_presence_prob[idx[:, :, 0], idx[:, :, 1], idx[:, :, 2]]
        assert winning_presence.shape == (batch_size, n_input_points)
        del idx

        # (B, O, M)
        vote_presence = mixing_logit[:, :-1] > mixing_logit[:, -1:]

        # (B, O+1, M)
        posterior_mixing_prob = F.softmax(posterior_mixing_logits_per_point, 1)
        del posterior_mixing_logits_per_point

        dummy_vote = self.dummy_vote.repeat(batch_size, 1, 1, 1)  # (B, 1, M, P)
        dummy_pres = torch.zeros([batch_size, 1, n_input_points], device=device)

        votes = torch.cat((self.vote, dummy_vote), 1)  # (B, O+1, M, P)
        presence = torch.cat([self.vote_presence_prob, dummy_pres], 1)  # (B, O+1, M)
        del dummy_vote
        del dummy_pres

        # (B, M, P)
        soft_winner = torch.sum(posterior_mixing_prob.unsqueeze(-1) * votes, 1)
        assert soft_winner.shape == (batch_size, n_input_points, dim_in)

        # (B, M)
        soft_winner_presence = torch.sum(posterior_mixing_prob * presence, 1)
        assert soft_winner_presence.shape == (batch_size, n_input_points)

        # (B, M, O)
        posterior_mixing_prob = posterior_mixing_prob[:, :-1].transpose(1, 2)

        # the first four votes belong to the square
        is_from_capsule = winning_vote_idx // self.n_votes

        return AttrDict(
            log_prob=mixture_log_prob_per_batch,
            vote_presence=vote_presence.float(),
            winner=winning_vote,
            winner_presence=winning_presence,
            soft_winner=soft_winner,
            soft_winner_presence=soft_winner_presence,
            posterior_mixing_prob=posterior_mixing_prob,
            mixing_log_prob=mixing_log_prob,
            mixing_logit=mixing_logit,
            is_from_capsule=is_from_capsule,
        )


class CapsuleObjectDecoder(nn.Module):
    def __init__(self, capsule_layer):
        """Builds the module.

        Args:
          capsule_layer: a capsule layer.
        """
        super().__init__()
        self.capsule_layer = capsule_layer

        self.dummy_vote = nn.Parameter(
            torch.zeros(1, 1, capsule_layer.n_votes, capsule_layer.n_transform_params),
            requires_grad=True
        )

    @property
    def n_obj_capsules(self):
        return self.capsule_layer.n_caps

    def forward(self, h, x, presence=None):
        """Builds the module.

        Args:
          h: Tensor of object encodings of shape [B, O, D].
          x: Tensor of inputs of shape [B, V, P]
          presence: Tensor of shape [B, V] or None; if it exists, it
            indicates which input points exist.

        Returns:
          A bunch of stuff.
        """
        device = next(iter(self.parameters())).device

        batch_size, n_caps = h.shape[:2]
        n_votes = x.shape[1]

        res = self.capsule_layer(h)
        res.vote = res.vote[..., :-1, :].view(batch_size, n_caps, n_votes, 6)

        vote_presence_prob = res.vote_presence
        likelihood = CapsuleLikelihood(
            vote=res.vote,
            scale=res.scale,
            vote_presence_prob=vote_presence_prob,
            dummy_vote=self.dummy_vote
        )
        likelihood.to(device)
        ll_res = likelihood(x, presence=presence)
        res.update(ll_res)
        del likelihood

        res.caps_presence_prob = torch.max(
            vote_presence_prob.view(batch_size, n_caps, n_votes),
            2
        )[0]

        return res


# prior sparsity loss
# l2(aggregated_prob - constant)
def capsule_l2_loss(caps_presence_prob,
                    num_classes: int,
                    within_example_constant=None,
                    **unused_kwargs):
    """Computes l2 penalty on capsule activations."""

    del unused_kwargs

    batch_size, num_caps = caps_presence_prob.shape  # B, O

    if within_example_constant is None:
        within_example_constant = float(num_caps) / num_classes

    between_example_constant = float(batch_size) / num_classes

    within_example = torch.mean(
        (caps_presence_prob.sum(1) - within_example_constant) ** 2)

    between_example = torch.mean(
        (caps_presence_prob.sum(0) - between_example_constant) ** 2)

    return within_example, between_example


# posterior sparsity loss
def capsule_entropy_loss(caps_presence_prob, k=1, **unused_kwargs):
    """Computes entropy in capsule activations."""
    del unused_kwargs

    # caps_presence_prob (B, O)

    within_prob = math_ops.normalize(caps_presence_prob, 1)  # (B, O)
    within_example = math_ops.cross_entropy_safe(within_prob,
                                                 within_prob * k)  # scalar

    total_caps_prob = torch.sum(caps_presence_prob, 0)  # (O, )
    between_prob = math_ops.normalize(total_caps_prob, 0)  # (O, )
    between_example = math_ops.cross_entropy_safe(between_prob,
                                                  between_prob * k)  # scalar
    # negate since we want to increase between example entropy
    return within_example, -between_example


# kl(aggregated_prob||uniform)
def neg_capsule_kl(caps_presence_prob, **unused_kwargs):
    del unused_kwargs

    num_caps = int(caps_presence_prob.shape[-1])
    return capsule_entropy_loss(caps_presence_prob, k=num_caps)


def sparsity_loss(loss_type, *args, **kwargs):
    """Computes capsule sparsity loss according to the specified type."""
    if loss_type == 'l2':
        sparsity_func = capsule_l2_loss
    elif loss_type == 'entropy':
        sparsity_func = capsule_entropy_loss
    elif loss_type == 'kl':
        sparsity_func = neg_capsule_kl
    else:
        raise ValueError(f"Invalid sparsity loss: {loss_type}")

    return sparsity_func(*args, **kwargs)
