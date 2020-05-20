import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


# Attention blocks code credits https://github.com/juho-lee/set_transformer

def qkv_attention(queries, keys, values, presence=None):
    """
    Transformer-like self-attention.

    Args:
      queries: Tensor of shape [B, N, d_k].
      keys: Tensor of shape [B, M, d_k].
      values: : Tensor of shape [B, M, d_v].
      presence: None or tensor of shape [B, M].

    Returns:
      Tensor of shape [B, N, d_v]
    """

    d_k = queries.shape[-1]

    # [B, N, d_k] x [B, d_k, M] = [B, N, M]
    routing = torch.matmul(queries, keys.transpose(1, 2))

    if presence is not None:
        routing -= (1. - presence.unsqueeze(-2)) * 1e32

    routing = F.softmax(routing / np.sqrt(d_k), -1)

    # every output is a linear combination of all inputs
    # [B, N, M] x [B, M, d_v] = [B, N, d_v]
    out = torch.matmul(routing, values)
    return out


class MultiHeadQKVAttention(nn.Module):
    """Multi-head version of Transformer-like attention."""

    def __init__(self, d_k, d_v, n_heads):
        super().__init__()
        self.d_k = d_k
        self.d_v = d_v
        self.n_heads = n_heads

        self.d_k_s = int(math.ceil(d_k / n_heads))
        self.d_v_s = int(math.ceil(d_v / n_heads))
        d_k_p = self.d_k_s * n_heads
        d_v_p = self.d_v_s * n_heads

        self.q_projector = nn.Linear(d_k, d_k_p)
        self.k_projector = nn.Linear(d_k, d_k_p)
        self.v_projector = nn.Linear(d_v, d_v_p)
        self.o_projector = nn.Linear(d_v_p, d_v)

    def forward(self, queries, keys, values, presence=None):
        """
        Multi-head transformer-like self-attention.

        Args:
          queries: Tensor of shape [B, N, d_k].
          keys: Tensor of shape [B, M, d_k].
          values: : Tensor of shape [B, M, d_v].
          presence: None or tensor of shape [B, M].

        Returns:
          Tensor of shape [B, N, d_v]
        """
        assert queries.shape[2] == keys.shape[2]
        assert keys.shape[1] == values.shape[1]
        if presence is not None:
            assert values.shape[:2] == presence.shape

        batch_size = queries.shape[0]

        q_p = self.q_projector(queries)  # (B, N, d_k_p)
        k_p = self.k_projector(keys)  # (B, M, d_k_p)
        v_p = self.v_projector(values)  # (B, M, d_v_p)

        q_s = q_p.split(self.d_k_s, -1)  # [(B, N, d_k_s)] * H
        k_s = k_p.split(self.d_k_s, -1)  # [(B, M, d_k_s)] * H
        v_s = v_p.split(self.d_v_s, -1)  # [(B, M, d_v_s)] * H

        q = torch.cat(q_s, 0)  # (H*B, N, d_k_s)
        k = torch.cat(k_s, 0)  # (H*B, M, d_k_s)
        v = torch.cat(v_s, 0)  # (H*B, M, d_v_s)

        if presence is not None:
            presence = presence.repeat(self.n_heads, 1)
        o = qkv_attention(q, k, v, presence)  # (H*B, N, d_v_s)
        o = o.split(batch_size, 0)  # [(B, N, d_v_s)] * H
        o = torch.cat(o, -1)  # (B, N, d_v_p)

        out = self.o_projector(o)  # (B, N, d_v)
        return out


class MAB(nn.Module):
    def __init__(self, d, n_heads, layer_norm=False):
        super().__init__()
        self.mqkv = MultiHeadQKVAttention(d_k=d, d_v=d, n_heads=n_heads)
        self.layer_norm = layer_norm
        if layer_norm:
            self.ln0 = nn.LayerNorm(d)
            self.ln1 = nn.LayerNorm(d)
        self.fc = nn.Linear(d, d)

    def forward(self, queries, keys, presence=None):
        h = self.mqkv(queries, keys, keys, presence)  # (B, N, d)
        h += queries  # (B, N, d)
        if self.layer_norm:
            h = self.ln0(h)  # (B, N, d)

        h += F.relu(self.fc(h))  # (B, N, d)
        if self.layer_norm:
            h = self.ln1(h)  # (B, N, d)

        return h


class SAB(nn.Module):
    def __init__(self, d, n_heads, layer_norm=False):
        super().__init__()
        self.mab = MAB(d=d, n_heads=n_heads, layer_norm=layer_norm)

    def forward(self, x, presence=None):
        return self.mab(x, x, presence)


class ISAB(nn.Module):
    def __init__(self, d, n_heads, n_inducing_points, layer_norm=False):
        super().__init__()
        self.mab0 = MAB(d=d, n_heads=n_heads, layer_norm=layer_norm)
        self.mab1 = MAB(d=d, n_heads=n_heads, layer_norm=layer_norm)
        self.I = nn.Parameter(torch.zeros(1, n_inducing_points, d),
                              requires_grad=True)
        with torch.no_grad():
            nn.init.xavier_uniform_(self.I)

    def forward(self, x, presence=None):
        batch_size = x.shape[0]
        H = self.mab0(self.I.repeat(batch_size, 1, 1), x, presence)
        return self.mab1(x, H)


class PMA(nn.Module):
    def __init__(self, d, n_heads, n_seeds, layer_norm=False):
        super().__init__()
        self.mab = MAB(d=d, n_heads=n_heads, layer_norm=layer_norm)
        self.S = nn.Parameter(torch.zeros(1, n_seeds, d), requires_grad=True)
        with torch.no_grad():
            nn.init.xavier_uniform_(self.S)

    def forward(self, x, presence=None):
        batch_size = x.shape[0]
        return self.mab(self.S.repeat(batch_size, 1, 1), x, presence)


class SetTransformer(nn.Module):
    """Permutation-invariant Transformer."""

    def __init__(self,
                 dim_in,
                 dim_hidden,
                 dim_out,
                 n_outputs,
                 n_layers,
                 n_heads,
                 layer_norm=False,
                 n_inducing_points: int = None):
        super().__init__()

        self.fc1 = nn.Linear(dim_in, dim_hidden)

        args = dict(
            d=dim_hidden,
            n_heads=n_heads,
            layer_norm=layer_norm,
        )
        if n_inducing_points is None:
            sab_fn = SAB
        else:
            args['n_inducing_points'] = n_inducing_points
            sab_fn = ISAB
        layers = [sab_fn(**args) for _ in range(n_layers)]
        self.sabs = nn.ModuleList(layers)

        self.fc2 = nn.Linear(dim_hidden, dim_out)

        self.pma = PMA(d=dim_out,
                       n_heads=n_heads,
                       n_seeds=n_outputs,
                       layer_norm=layer_norm)

    def forward(self, x, presence=None):
        h = self.fc1(x)

        for sab in self.sabs:
            h = sab(h, presence)

        z = self.fc2(h)

        return self.pma(z, presence)
