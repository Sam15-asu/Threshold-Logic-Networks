#FINAL CODE
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import einops
import math


class WeightedThresholdGate(nn.Module):
    """
    Weighted-sum threshold gate layer (perceptron-style).
      For each output j:
        select fan_in inputs x[idx[j, :]]
        z_j = sum_i w[j,i] * x[idx[j,i]] - theta[j]
        y_j = sigmoid(s * z_j) during training
            = 1[z_j >= 0] at eval (hard step)

    Input:  x shape (B, in_dim) or (B, in_dim, L)
    Output: y shape (B, out_dim) or (B, out_dim, L)
    """
    def __init__(self,
                 in_dim: int,
                 out_dim: int,
                 fan_in: int = 8,
                 weight_init: str = "xavier_uniform",   # "kaiming_uniform" | "xavier_uniform" | "normal_small"
                 theta_init: str = "mean_abs_w",               # "zero" | "mean_abs_w" | "median_abs_w"
                 s_init: float = 1.0,
                 layer_id: int = None):
        super().__init__()
        assert 1 <= fan_in <= in_dim
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.fan_in = fan_in
        self.layer_id = layer_id

        # --- fixed sparse wiring: choose fan_in distinct inputs per output ---
        self.register_buffer("idx", self._make_connections(in_dim, out_dim, fan_in).long())  # (out_dim, fan_in)

        # --- learnable params: weights, thresholds, and global sigmoid slope s ---
        self.w     = nn.Parameter(torch.empty(out_dim, fan_in))
        self.theta = nn.Parameter(torch.zeros(out_dim))
        self.s_raw  = nn.Parameter(torch.full((out_dim,), float(s_init)))  # unconstrained; will map to s_j > 0

        self._init_weights(self.w, scheme=weight_init)
        self._init_theta(theta_init)

    # ---------- inits & helpers ----------
    @staticmethod
    def _make_connections(in_dim: int, out_dim: int, fan_in: int) -> torch.Tensor:
        rows = []
        for _ in range(out_dim):
            perm = torch.randperm(in_dim)        # no replacement within a unit
            rows.append(perm[:fan_in])
        return torch.stack(rows, dim=0)          # (out_dim, fan_in)

    def _init_weights(self, w: torch.Tensor, scheme: str):
        if scheme == "kaiming_uniform":
            nn.init.kaiming_uniform_(w, a=math.sqrt(5))
        elif scheme == "xavier_uniform":
            nn.init.xavier_uniform_(w)
        elif scheme == "normal_small":
            nn.init.normal_(w, mean=0.0, std=0.1)
        else:
            raise ValueError(f"Unknown weight_init: {scheme}")

    def _init_theta(self, mode: str):
        with torch.no_grad():
            if mode == "zero":
                self.theta.zero_()
            elif mode == "mean_abs_w":
                t = self.w.abs().mean(dim=1) * (self.fan_in / 2.0)
                self.theta.copy_(t)
            elif mode == "median_abs_w":
                t = self.w.abs().median(dim=1).values * (self.fan_in / 2.0)
                self.theta.copy_(t)
            else:
                raise ValueError(f"Unknown theta_init: {mode}")
            
    def _slope(self) -> torch.Tensor:
        # map to strictly positive slopes; +eps avoids zero
        return F.softplus(self.s_raw) + 1e-6  # shape: (out_dim,)
    
    # Optional regularizers you might add to your loss
    def saturation_penalty(self, z: torch.Tensor) -> torch.Tensor:
        s = torch.sigmoid(self.s * z)
        return (s * (1 - s)).mean()

    def margin_penalty(self, z: torch.Tensor, margin: float = 1.0) -> torch.Tensor:
        return F.relu(margin - z.abs()).mean()

    # ---------- forward ----------
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: (B, in_dim) or (B, in_dim, L)
        returns: (B, out_dim) or (B, out_dim, L)
        """
        assert x.dim() in (2, 3), "x must be (B, in_dim) or (B, in_dim, L)"
        B = x.size(0)
        extra = x.shape[2:]  # () or (L,)

        # Gather wired inputs for each output: (B, out_dim, fan_in, *extra)
        x_sel = x[:, self.idx.reshape(-1), ...]                 # (B, out_dim*fan_in, *extra)
        x_sel = x_sel.contiguous().view(B, self.out_dim, self.fan_in, *extra)

        # Weighted sum per output unit
        w = self.w.contiguous().view(1, self.out_dim, self.fan_in, *([1] * len(extra)))
        sum_ = (x_sel * w).sum(dim=2)                              # (B, out_dim, *extra)

        # Subtract threshold
        theta = self.theta.view(1, self.out_dim, *([1] * len(extra)))
        z = sum_ - theta                                           # (B, out_dim, *extra)

        # Per-filter slopes (broadcast to z's shape)
        s = self._slope().view(1, self.out_dim, *([1] * len(extra)))

        if self.training:
            y_soft = torch.sigmoid(s * z)                  # element-wise, global slope s
            return y_soft
        else:
            # At eval, use hard thresholding
            return (z >= 0).to(x.dtype)



class Conv(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1,
                 *, fan_in=8, theta_init="mean_abs_w", s_init=1, **kwargs):
        super().__init__(**kwargs)
        self.kernel_size = kernel_size
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.stride = stride
        self.padding = padding

        in_dim = (kernel_size ** 2) * in_channels
        fan_in = min(fan_in, in_dim)

        self.gatebank = WeightedThresholdGate(
            in_dim=in_dim,
            out_dim=out_channels,       # <— all kernels at once
            fan_in=fan_in,
            weight_init="kaiming_uniform",
            theta_init=theta_init,
            s_init=s_init
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, _, H, W = x.shape
        x = F.pad(x, (self.padding, self.padding, self.padding, self.padding), mode='constant', value=0)
        Hout = (H + 2*self.padding - self.kernel_size) // self.stride + 1
        Wout = (W + 2*self.padding - self.kernel_size) // self.stride + 1

        patches = F.unfold(x, kernel_size=self.kernel_size, stride=self.stride)   # (B, in_dim, L)
        y = self.gatebank(patches)                                                # (B, C_out, L)
        y = einops.rearrange(y, 'b c (h w) -> b c h w', h=Hout, w=Wout)
        return y

