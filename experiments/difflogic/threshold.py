import torch
import numpy as np
import math
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.parameter
from torch.nn import init
import torch.optim as optim
#from main_baseline import InferenceDebugConfig

import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function


class ThresholdLayer(nn.Module):
    """
    Sparse threshold layer with fixed fan-in (= num_active) per neuron.
    Computes (per batch element):
        y = sum_t Wk[row, t] * x[idx[row, t]]  - bias[row]

    Constructor matches your old signature:
        in_dim, out_dim, num_active, bias=True, device='cuda',
        grad_factor=1., implementation='python', layer_id=None

    Back-compat aliases provided:
      - num_neurons (== out_dim)
      - num_weights (== out_dim)
      - effective_weight (dense (out_dim, in_dim) view)
    """

    class GradFactor(torch.autograd.Function):
        @staticmethod
        def forward(ctx, x, factor: float):
            ctx.factor = factor
            return x
        @staticmethod
        def backward(ctx, g):
            return g * ctx.factor, None

    def __init__(self,
                 in_dim: int,
                 out_dim: int,
                 num_active: int,
                 bias: bool = True,
                 device: str = 'cuda',
                 grad_factor: float = 1.,
                 implementation: str = 'python',   # kept for API compatibility
                 layer_id: int = None,
                 mask: torch.Tensor = None):        # optional: reuse exact mask
        super().__init__()

        assert num_active > 0, "num_active must be > 0"
        self.in_dim       = in_dim
        self.out_dim      = out_dim
        self.num_active   = num_active
        self.device_hint  = device
        self.grad_factor  = grad_factor
        self.layer_id     = layer_id
        self.implementation = implementation  # not used; kept to match old API

        # Back-compat aliases expected by your old code
        self.num_neurons = out_dim
        self.num_weights = out_dim

        # ---- mask & idx (fixed connectivity) ----
        if mask is None:
            mask = self._create_mask_exact(out_dim, in_dim, num_active)
        else:
            assert mask.shape == (out_dim, in_dim), "mask must be (out_dim, in_dim)"
            rowsums = mask.to(torch.bool).sum(dim=1)
            if not torch.all(rowsums == num_active):
                raise ValueError("each mask row must have exactly num_active ones")

        self.register_buffer("mask", mask.to(dtype=torch.float32))
        self.register_buffer("idx", self._mask_to_idx(self.mask))  # (out_dim, num_active)

        # ---- trainable parameters (only active weights are stored) ----
        self.weight_k = nn.Parameter(torch.empty(out_dim, num_active))
        if bias:
            self.bias = nn.Parameter(torch.empty(out_dim))
        else:
            self.register_parameter("bias", None)

        self.reset_parameters()
        self.debug = False  # optional flag like your old code

    # ---------- Back-compat property: dense effective weight ----------
    @property
    def effective_weight(self) -> torch.Tensor:
        """Dense (out_dim, in_dim) view with zeros at inactive positions."""
        return self.to_dense_weight()

    # ----------------------- public helpers -----------------------

    @torch.no_grad()
    def port_from_dense(self,
                        weight_old: torch.Tensor,   # (out_dim, in_dim)
                        bias_old:   torch.Tensor,   # (out_dim,) or None
                        mask_old:   torch.Tensor):  # (out_dim, in_dim)
        """Copy params from your previous dense-masked layer (identical outputs)."""
        assert weight_old.shape == (self.out_dim, self.in_dim)
        assert mask_old.shape   == (self.out_dim, self.in_dim)
        if self.bias is not None:
            assert bias_old is not None and bias_old.shape == (self.out_dim,)

        same = (mask_old.to(self.mask.dtype).to(self.mask.device) == self.mask).all()
        if not bool(same):
            raise ValueError("mask_old must match this layer's mask exactly.")

        for r in range(self.out_dim):
            cols = self.idx[r]
            self.weight_k[r].copy_(weight_old[r, cols])

        if self.bias is not None and bias_old is not None:
            self.bias.copy_(bias_old)

    def to_dense_weight(self) -> torch.Tensor:
        """Materialize a dense (out_dim, in_dim) with zeros at inactive positions."""
        W = torch.zeros(self.out_dim, self.in_dim,
                        device=self.weight_k.device, dtype=self.weight_k.dtype)
        row = torch.arange(self.out_dim, device=self.idx.device).unsqueeze(1).expand_as(self.idx)
        W[row, self.idx] = self.weight_k
        return W

    # ----------------------- module API -----------------------

    def reset_parameters(self):
        # Xavier-like init using fan_in = num_active
        bound = 1.0 / math.sqrt(self.num_active) if self.num_active > 0 else 0.0
        with torch.no_grad():
            self.weight_k.uniform_(-bound, bound)
            if self.bias is not None:
                self.bias.uniform_(-bound, bound)

    def forward(self, x: torch.Tensor, verbose: bool = False) -> torch.Tensor:
        if self.grad_factor != 1.:
            x = ThresholdLayer.GradFactor.apply(x, self.grad_factor)

        B = x.shape[0]

        # 1) Expand x along dim=1 to match out_dim
        x_exp = x.unsqueeze(1).expand(B, self.out_dim, self.in_dim)  # (B, out_dim, in_dim)

        # 2) Gather per-neuron active inputs
        #    idx_exp: (B, out_dim, num_active)
        idx_exp = self.idx.unsqueeze(0).expand(B, -1, -1)
        x_g = x_exp.gather(2, idx_exp)  # (B, out_dim, num_active)

        # 3) Multiply-and-sum over the small fan-in
        y = (x_g * self.weight_k.unsqueeze(0)).sum(dim=-1)  # (B, out_dim)

        # 4) Subtract bias (threshold semantics)
        if self.bias is not None:
            y = y - self.bias

        if verbose or self.debug:
            with torch.no_grad():
                print(f"[ThresholdLayer id={self.layer_id}] x:{tuple(x.shape)} "
                    f"x_g:{tuple(x_g.shape)} y:{tuple(y.shape)}")

        return y

    def extra_repr(self):
        return (f'in_dim={self.in_dim}, out_dim={self.out_dim}, '
                f'num_active={self.num_active}, '
                f'bias={self.bias is not None}, '
                f'grad_factor={self.grad_factor}, '
                f'impl=indexed, '
                f'layer_id={self.layer_id}')

    # ----------------------- internals -----------------------

    @staticmethod
    def _mask_to_idx(mask: torch.Tensor) -> torch.LongTensor:
        mask_b = mask.to(torch.bool)
        out_dim, _ = mask_b.shape
        rows = []
        for r in range(out_dim):
            cols = torch.nonzero(mask_b[r], as_tuple=True)[0]
            rows.append(cols)
        k = rows[0].numel()
        assert all(c.numel() == k for c in rows), "All rows must have identical num_active."
        return torch.stack(rows, dim=0).to(torch.long)

    @staticmethod
    def _create_mask_exact(out_dim: int, in_dim: int, num_active: int) -> torch.Tensor:
        """Create (out_dim, in_dim) mask with EXACTLY num_active ones per row."""
        mask = torch.zeros(out_dim, in_dim, dtype=torch.float32)
        cols = torch.randperm(in_dim)
        # Round-robin assign one per column where possible
        for i, c in enumerate(cols):
            r = i % out_dim
            if mask[r].sum() < num_active:
                mask[r, c] = 1.0
        # Fill each row to num_active
        for r in range(out_dim):
            need = int(num_active - mask[r].sum().item())
            if need > 0:
                zeros = (mask[r] == 0).nonzero(as_tuple=True)[0]
                pick = zeros[torch.randperm(zeros.numel())[:need]]
                mask[r, pick] = 1.0
        assert torch.all(mask.sum(dim=1) == num_active), "Row(s) not at exact num_active."
        return mask

class ThreshooldLayer(nn.Module):
    __constants__ = ['bias']
    """
    The core module for Threshold Layer. Provides a differentiable Threshold gate layer.
    It includes a trainable scalar `s` (here stored in buffer `p`) that scales the output and is used in the
    activation function applied after the linear transformation:
      - In training: sigmoid(s * x)
      - In evaluation: heaviside(s * x)
    """
    def __init__(
            self,
            in_dim: int,
            out_dim: int,
            num_active: int, 
            bias: bool = True,
            device: str = 'cuda',
            grad_factor: float = 1.,
            implementation: str = 'python',
            layer_id: int = None,
    ):
        """
        Args:
            in_dim (int): Input dimensionality.
            out_dim (int): Output dimensionality.
            num_active (int): Number of active (nonzero) connections per output neuron.
            bias (bool): Whether to include a bias term.
            device (str): 'cuda' or 'cpu'.
            grad_factor (float): Gradient scaling factor.
            implementation (str): Implementation choice (only 'python' supported).
            layer_id (int): Optional layer id.
        """
        super(ThresholdLayer, self).__init__()

        self.in_dim = in_dim
        self.out_dim = out_dim
        self.num_active = num_active
        self.device = device
        self.grad_factor = grad_factor
        self.layer_id = layer_id 
        self.implementation = implementation
        assert self.implementation in ['python'], (self.implementation)

        self.num_neurons = out_dim
        self.num_weights = out_dim

        # Full trainable weight matrix (like nn.Linear)
        self.weight = nn.Parameter(torch.Tensor(out_dim, in_dim))
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_dim))
        else:
            self.register_parameter('bias', None)

        # Create a fixed binary mask (non-trainable) of the same shape.
        # Each row has exactly `num_active` ones and zeros elsewhere,
        # and every column is activated at least once.
        mask = self.create_mask(self.out_dim, self.in_dim, self.num_active)
        self.register_buffer('mask', mask)

        self.reset_parameters()

        

    @staticmethod
    def create_mask(out_dim, in_dim, num_active):
        """
        Create a (out_dim x in_dim) mask such that:
          - Each row has exactly num_active ones.
          - Each column is activated (has at least one one) overall.
          - When removing extra ones in a row, do not remove a one from a column 
            that would otherwise drop to zero.
        """
        # Create an empty mask with zeros.
        mask = torch.zeros(out_dim, in_dim)
        
        # --- Step 1: Ensure every column gets at least one 1 ---
        for col in range(in_dim):
            row_idx = torch.randint(low=0, high=out_dim, size=(1,)).item()
            mask[row_idx, col] = 1.0

        # --- Step 2: Fill each row so that it has exactly num_active ones ---
        for row in range(out_dim):
            current_active = int(mask[row].sum().item())
            
            if current_active < num_active:
                # Find indices where the row is still 0.
                zero_indices = (mask[row] == 0).nonzero(as_tuple=True)[0]
                num_to_add = num_active - current_active
                # Randomly select indices from zero_indices (without replacement)
                perm = torch.randperm(len(zero_indices))
                selected = zero_indices[perm[:num_to_add]]
                mask[row, selected] = 1.0
            
            elif current_active > num_active:
                # If there are extra ones, remove some without deactivating a column.
                one_indices = (mask[row] == 1).nonzero(as_tuple=True)[0]
                num_to_remove = current_active - num_active
                candidates = []
                for idx in one_indices:
                    # Only remove if column has more than one active entry.
                    if mask[:, idx].sum() > 1:
                        candidates.append(idx)
                if len(candidates) >= num_to_remove:
                    perm = torch.randperm(len(candidates))
                    selected = [candidates[i] for i in perm[:num_to_remove]]
                else:
                    selected = candidates
                mask[row, selected] = 0.0

        return mask

    def reset_parameters(self):
        # Initialize the weight matrix with Xavier uniform initialization.
        init.xavier_uniform_(self.weight)
        
        # Initialize bias with a uniform distribution based on num_active (active connections)
        if self.bias is not None:
            fan_in = self.num_active
            bound = 1 / math.sqrt(fan_in) if fan_in > 0 else 0
            init.uniform_(self.bias, -bound, bound)

    @property
    def effective_weight(self):
        """
        Returns the effective weight matrix, which is the element-wise product of the
        trainable weight matrix and the fixed binary mask.
        """
        return self.weight * self.mask
    
    def forward(self, x):
        
        if self.grad_factor != 1.:
            x = GradFactor.apply(x, self.grad_factor)
        if self.implementation == 'python':
            return self.forward_python(x)
        else:
            raise ValueError(self.implementation)

    def forward_python(self, x):
        # If debug flag is set, print detailed information.
        #print("input datatype:", x.dtype)
        if hasattr(self, "debug") and self.debug:
            print(" ")
            print(f"--- Debug Info for ThresholdLayer (layer id {self.layer_id}) ---")
            if self.layer_id == 2 or self.layer_id == 3:
                print("Input max:", x.max().item(), "min:", x.min().item())
                if not self.training:
                    print("Input (x):", x)
            
            # Effective weight information
            effective_weight = self.effective_weight
            #print("Effective weight shape:", effective_weight.shape)
            print("Effective weight max:", effective_weight.max().item(), "min:", effective_weight.min().item())
            #print("Effective weight:", effective_weight)
        
        # Compute the linear transformation
        y = F.linear(x, self.effective_weight, None) - self.bias

        if hasattr(self, "debug") and self.debug:
            #print("Output shape (y):", y.shape)
            print("Output max:", y.max().item(), "min:", y.min().item())
            print(" ")
        
        return y
    
    def print_linear_operation(self, x_in, weight, bias):
        """
        Prints a string representation of the linear transformation for each neuron,
        for the first sample in the batch.
        
        For each neuron, prints an expression like:
          (w1 * x1) + (w2 * x2) + ... - bias
        """
        # Use the first sample in the batch for demonstration.
        x_sample = x_in[0]
        for neuron_idx in range(weight.shape[0]):
            terms = []
            for j in range(weight.shape[1]):
                w_val = weight[neuron_idx, j].item()
                x_val = x_sample[j].item()
                terms.append(f"({w_val:.2f} * {x_val:.2f})")
            neuron_str = " + ".join(terms)
            if bias is not None:
                neuron_str += f" - {bias[neuron_idx].item():.2f}"
            print(f"Neuron {neuron_idx} transformation: {neuron_str}")
 
    def extra_repr(self):
        return 'in_dim={}, out_dim={}, num_active_connections={}, bias={}'.format(
            self.in_dim, self.out_dim, self.num_active, self.bias is not None, 'train' if self.training else 'eval')

class CustomSigmoid(nn.Module):
    """
    A customized sigmoid activation function that applies a trainable scaling parameter s.
    
    During training, it computes:
        output = sigmoid(s * x)
    
    During evaluation, it computes a hard threshold (Heaviside step):
        output = 1 if s * x > 0, else 0
        
    When debug is enabled (e.g., at evaluation intervals), it prints:
      - The value and shape of the scaling parameter s.
      - The output of sigmoid(s * x) (raw sigmoid activation).
      - In eval mode, the thresholded output.
    """
    def __init__(self, init_scale: float = 1.0, layer_id: int = None):
        """
        Args:
            init_scale (float): Initial value for the trainable scaling parameter s.
        """
        super(CustomSigmoid, self).__init__()
        self.s = nn.Parameter(torch.tensor(init_scale, dtype=torch.float32))
        self.layer_id = layer_id
        
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        scaled_x = self.s * x
        # If debug flag is enabled, print debugging info.
        if hasattr(self, "debug") and self.debug:
            print("--- Debug Info for CustomSigmoid ---")
            print("Sigmoid Input max:", x.max().item(), "min:", x.min().item())
            print("Parameter s value:", self.s.item())
            # Compute the sigmoid output for debugging.
            sig_output = torch.sigmoid(scaled_x)
            print("Sigmoid Output max:", sig_output.max().item(), "min:", sig_output.min().item())
    
        # Return outputs based on mode.
        if self.training:
            sigmoid_output = torch.sigmoid(scaled_x)
            return sigmoid_output
        
        else:
            y_scaled = (scaled_x >= 0).float()
            #print("output of sigmoid in eval",y_scaled)
            return y_scaled
        
    def extra_repr(self) -> str:
        return f'scale={self.s.item()}'
    
class CustomSigmoid2(nn.Module):
    """
    A customized sigmoid activation function that applies a fixed scaling parameter s.
    
    During training, it computes:
        output = sigmoid(s * x)
    
    During evaluation, it computes a hard threshold (Heaviside step):
        output = 1 if s * x > 0, else 0
        
    When debug is enabled, it prints:
      - The value and shape of the scaling parameter s.
      - The output of sigmoid(s * x) (raw sigmoid activation).
      - In eval mode, the thresholded output.
    """
    def __init__(self, init_scale: float = 1.0, step: float = 0.0007):
        """
        Args:
            init_scale (float): The fixed scaling value.
        """
        super(CustomSigmoid2, self).__init__()
        # Register s as a fixed buffer (a constant tensor in fp32)
        self.register_buffer('p', torch.tensor(init_scale, dtype=torch.float32))
        self.step = step

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        scaled_x = self.p * x
        # If debug flag is enabled, print debugging info.
        if hasattr(self, "debug") and self.debug:
            print("--- Debug Info for CustomSigmoid2 ---")
            print("Sigmoid2 Input max:", x.max().item(), "min:", x.min().item())
            print("Parameter p value:", self.p.item())
            # Compute the sigmoid output for debugging.
            sig_output = torch.sigmoid(scaled_x)
            print("Sigmoid2 Output max:", sig_output.max().item(), "min:", sig_output.min().item())
    
        # Return outputs based on mode.
        if self.training:
            self.p = self.p + self.step
            sigmoid_output = torch.tanh(scaled_x)

            return sigmoid_output
        else:
            #print("eval mode")
            y_scaled = (scaled_x >= 0).float()
            return y_scaled
        
    def extra_repr(self) -> str:
        return f'scale={self.p.item()}'
 
        

class BinaryStep(nn.Module):
    def __init__(self):
        """
        A custom activation function that returns 1 for inputs > 0 and 0 for inputs <= 0,
        but uses the Straight-Through Estimator (STE) to pass gradients during backpropagation.
        """
        super(BinaryStep, self).__init__()

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        # Use our custom STE autograd function.
        return BinaryStepSTE.apply(input)

    def extra_repr(self) -> str:
        return ''

class BinaryStepSTE(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        # Forward: return 1 if input > 0, else 0.
        # We use torch.where to get a binary output.
        return torch.where(input > 0,
                           torch.tensor(1.0, device=input.device, dtype=input.dtype),
                           torch.tensor(0.0, device=input.device, dtype=input.dtype))
    
    @staticmethod
    def backward(ctx, grad_output):
        # Backward: Straight-Through Estimator (STE).
        # Here, we pass the gradient through unchanged.
        # Optionally, you might clip or scale the gradient.
        return grad_output    

class GroupSum(torch.nn.Module):
    """
    The GroupSum module.
    """
    def __init__(self, k: int, tau: float = 1., device='cuda'):
        """

        :param k: number of intended real valued outputs, e.g., number of classes
        :param tau: the (softmax) temperature tau. The summed outputs are divided by tau.
        :param device:
        """
        super().__init__()
        self.k = k
        self.tau = tau
        self.device = device

    def forward(self, x):

        if hasattr(self, "debug") and self.debug:
            print(" ")
            print(" -----Evaluating GroupSum-----")
            #print("Input x shape:", x.shape)
            print("Input x max:", x.max().item(), "min:", x.min().item())

        assert x.shape[-1] % self.k == 0, (x.shape, self.k)
        #print("x.shape:", x.shape)
        #print("x.shape[-1]:", x.shape[-1])
        #print("x.shape[-1] // self.k:", x.shape[-1] // self.k)
        out = x.reshape(*x.shape[:-1], self.k, x.shape[-1] // self.k).sum(-1) / self.tau
        #print("out:", out)
        if hasattr(self, "debug") and self.debug:
            #print("Output shape:", out.shape)
            print("Output max:", out.max().item(), "min:", out.min().item())
            print("----------------------------------------------------------------------------------------------------------------------------------------------------------------")
            print("   ")
        
        return out
    print("----------------------------------------------------------------------------")
        
    def extra_repr(self):
        return 'k={}, tau={}'.format(self.k, self.tau)
    
class GradFactor(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, f):
        ctx.f = f
        return x

    @staticmethod
    def backward(ctx, grad_y):
        return grad_y * ctx.f, None