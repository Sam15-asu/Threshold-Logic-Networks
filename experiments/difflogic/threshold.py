import torch
import numpy as np
import math
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.parameter
from torch.nn import init
import torch.optim as optim
#from main_baseline import InferenceDebugConfig

class ThresholdLayer(nn.Module):
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
            print("Input max:", x.max().item(), "min:", x.min().item())
    
            
            # Effective weight information
            effective_weight = self.effective_weight
            #print("Effective weight shape:", effective_weight.shape)
            #print("Effective weight max:", effective_weight.max().item(), "min:", effective_weight.min().item())
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
            print(f"--- Debug Info for CustomSigmoid (layer id {self.layer_id})---")
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