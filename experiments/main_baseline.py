import argparse
import random
from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+

now_phx = datetime.now(ZoneInfo("America/Phoenix"))
print(now_phx.strftime("%Y-%m-%d %H:%M:%S %Z"))

import numpy as np
import pandas as pd
import torch
from torchvision import datasets, transforms
from tqdm import tqdm
import torch.nn as nn
import csv
import os
os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
from torch.optim.lr_scheduler import ReduceLROnPlateau
from difflogic.threshold import ThresholdLayer, CustomSigmoid, CustomSigmoid2, GroupSum
import difflogic.binarization as bin
from difflogic.connections import Conv
from pipeline import run_full_logic_pipeline

torch.set_num_threads(1)
torch.set_printoptions(threshold=float('inf'), linewidth=200)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print("device:", device)


BITS_TO_TORCH_FLOATING_POINT_TYPE = {
    16: torch.float16,
    32: torch.float32,
    64: torch.float64
}
    
class CustomModel(torch.nn.Module):
    """
    A wrapper around the model to enable verbose output during forward passes.
    """
    def __init__(self, model):
        super(CustomModel, self).__init__()
        self.model = model

    def forward(self, x, verbose=False):
        if verbose:
            print()
        y = self.model(x)
        if verbose:
            print()
        return y

    
def load_dataset(args):
    """ 
    Load the specified dataset, prepares(binarizes) the data and return DataLoaders for training, validation, 
    and testing for further analysis.
    """
    validation_loader = None
    transform = transforms.Compose([transforms.ToTensor(), transforms.Lambda(lambda x: torch.flatten(x))])
    

    if 'cifar10' in args.dataset:

        # 1) Load CIFAR-10 (no flatten; values in [0,1])
        transform_plain = transforms.ToTensor()
        train_dataset = datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_plain)
        test_dataset  = datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_plain)

        # 2) Grab the whole train set once to fit thresholds (you can fit on a large subset if VRAM/RAM is tight)
        big_loader_train = torch.utils.data.DataLoader(
            dataset=train_dataset, batch_size=len(train_dataset), shuffle=True
        )
        x_all, y_all = next(iter(big_loader_train))             # x_all: (N=50000, 3, 32, 32)

        # 3) Fit a 2-bit DistributiveThermometer (feature-wise quantiles across the batch dimension)
        NUM_BITS = 2
        thermometer = bin.DistributiveThermometer(num_bits=NUM_BITS, feature_wise=True).fit(x_all)

        # 4) Binarize → (N, 3, 32, 32, 2), then move bit-plane axis into channels (channel-blocked) → (N, 6, 32, 32)
        x_bits = thermometer.binarize(x_all)                    # (N, 3, 32, 32, 2)
        x_bin  = x_bits.permute(0, 1, 4, 2, 3).reshape(x_bits.size(0), 3*NUM_BITS, 32, 32)  # (N, 6, 32, 32)

        # same for test split
        big_loader_test = torch.utils.data.DataLoader(
            dataset=test_dataset, batch_size=len(test_dataset), shuffle=False
        )
        x_test_all, y_test_all = next(iter(big_loader_test))
        x_test_bits = thermometer.binarize(x_test_all)          # (Ntest, 3, 32, 32, 2)
        x_test_bin  = x_test_bits.permute(0, 1, 4, 2, 3).reshape(x_test_bits.size(0), 3*NUM_BITS, 32, 32)  # (Ntest, 6, 32, 32)

        # 5) Wrap into TensorDatasets
        train_dataset_bin = torch.utils.data.TensorDataset(x_bin, y_all)
        test_dataset_bin  = torch.utils.data.TensorDataset(x_test_bin, y_test_all)

        # 6) Final conv-ready loaders (batches will be (B, 6, 32, 32))
        train_loader = torch.utils.data.DataLoader(
            train_dataset_bin, batch_size=args.batch_size, shuffle=True,
            pin_memory=True, drop_last=False, num_workers=4
        )
        test_loader = torch.utils.data.DataLoader(
            test_dataset_bin, batch_size=args.batch_size, shuffle=False,
            pin_memory=True, drop_last=False, num_workers=4
        )
        # After binarization
        print(f"x_bin (train)  shape: {tuple(x_bin.shape)}")        # e.g., (50000, 6, 32, 32) if NUM_BITS=2
        print(f"x_test_bin     shape: {tuple(x_test_bin.shape)}")   # e.g., (10000, 6, 32, 32)

        # Sanity checks
        print("train channels =", x_bin.shape[1], " expected =", 3*NUM_BITS)
        print("test  channels =", x_test_bin.shape[1], " expected =", 3*NUM_BITS)
        print("values in {0,1}? min/max train:", x_bin.min().item(), x_bin.max().item())

        # After building DataLoaders: peek at one batch
        xb, yb = next(iter(train_loader))
        print(f"train batch shape: {tuple(xb.shape)} labels: {tuple(yb.shape)}")  # (B, 3*NUM_BITS, 32, 32), (B,)
        xb_t, yb_t = next(iter(test_loader))
        print(f"test  batch shape: {tuple(xb_t.shape)} labels: {tuple(yb_t.shape)}")

    elif 'fashionMNIST' in args.dataset:
        train_dataset = datasets.FashionMNIST(root='./data', train=True, download=True, transform=transform)
        test_dataset = datasets.FashionMNIST(root='./data', train=False, download=True, transform=transform)

        train_loader_bin = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=len(train_dataset), shuffle=True)
        test_loader_bin = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=len(test_dataset), shuffle=False)

        x_train, y_train = next(iter(train_loader_bin))
        x_test, y_test = next(iter(test_loader_bin))

        # Binarize with distributive thermometer
        thermometer = bin.DistributiveThermometer(7).fit(x_train)
        x_train = thermometer.binarize(x_train).flatten(start_dim=1)
        x_test = thermometer.binarize(x_test).flatten(start_dim=1)
        # Create new datasets
        train_dataset_bin = torch.utils.data.TensorDataset(x_train, y_train)
        test_dataset_bin = torch.utils.data.TensorDataset(x_test, y_test)

        # Create DataLoaders with your desired batch sizes
        train_loader = torch.utils.data.DataLoader(train_dataset_bin, batch_size=args.batch_size, shuffle=True,pin_memory=True, drop_last=False, num_workers=4)
        test_loader = torch.utils.data.DataLoader(test_dataset_bin, batch_size=args.batch_size, shuffle=False, pin_memory=True, drop_last=False)

    elif 'mnist' in args.dataset:
        # 1. Load MNIST as usual, but only ToTensor (no flatten)
        transform = transforms.ToTensor()  
        train_dataset = datasets.MNIST(root='./data',train=True,download=True,transform=transform)
        test_dataset = datasets.MNIST(root='./data',train=False,download=True,transform=transform)

        # 2. Grab the whole train set once (to fit your 1‑bit thermometer)
        big_loader = torch.utils.data.DataLoader(dataset=train_dataset,batch_size=len(train_dataset),shuffle=True)
        x_all, y_all = next(iter(big_loader))   # x_all.shape == [N, 1, 28, 28]

        # 3. Fit a 1‑bit DistributiveThermometer on those images
        thermometer = bin.DistributiveThermometer(1).fit(x_all)

        # 4. Binarize—but DON’T flatten—so shape stays (N, 1, 28, 28)
        x_bin = thermometer.binarize(x_all)     # → [N, 1, 28, 28]
        # 1) Drop the singleton 1‑channel dim
        x_bin = x_bin.squeeze(-1)              # now shape [N, 28, 28, 2]

        # same for test
        test_loader_big = torch.utils.data.DataLoader(dataset=test_dataset,batch_size=len(test_dataset),shuffle=False)

        x_test_all, y_test_all = next(iter(test_loader_big))
        x_test_bin = thermometer.binarize(x_test_all)  # → (Ntest, 1, 28, 28, 2)
        x_test_bin = x_test_bin.squeeze(-1)             # → (Ntest, 28, 28, 2)
    
        # 5. Wrap back into TensorDatasets
        train_dataset_bin = torch.utils.data.TensorDataset(x_bin, y_all)
        test_dataset_bin  = torch.utils.data.TensorDataset(x_test_bin, y_test_all)

        # 6. Final conv‑ready loaders
        train_loader = torch.utils.data.DataLoader(
            train_dataset_bin,batch_size=args.batch_size,shuffle=True,pin_memory=True, drop_last=False,num_workers=4)
        test_loader = torch.utils.data.DataLoader(test_dataset_bin,batch_size=args.batch_size,shuffle=False,pin_memory=True,drop_last=False)

    else:
        raise NotImplementedError(f'The data set {args.dataset} is not supported!')

    return train_loader, validation_loader, test_loader


def load_n(loader, n):
    """
    Yield exactly `n` batches from a DataLoader-like iterable.

    This is a small helper to take the first `n` batches from potentially
    infinite or long-running loaders (e.g. for a fixed number of training
    iterations independent of dataset size).

    Parameters
    ----------
    loader : Any iterable that yields batches (e.g. a PyTorch DataLoader).
    n : Number of batches to yield in total across all iterations.

    Yields
    ------
    batch: The next batch produced by `loader`, up to `n` batches in total.
    """
    i = 0
    while i < n:
        for x in loader:
            yield x
            i += 1
            if i == n:
                break


def input_dim_of_dataset(dataset):
    """
    Return the input dimension for the specified dataset.
    """
    return {
        'mnist': 1568,
        'cifar10': 3 * 32 * 32 * 10,
        'fashionMNIST': 5488
    }[dataset]


def num_classes_of_dataset(dataset):
    """
    Return the number of classes for the specified dataset.
    """
    return {
        'mnist': 10,
        'cifar10': 10,
        'fashionMNIST': 10
    }[dataset]

def get_model(args):
    """
    Create and return the model, loss function, optimizer, and scheduler based on the provided arguments.
    Model architecture is built according to the specified dataset and parameters.
    Loss function is CrossEntropyLoss.
    Optimizer is Adam with specified learning rate and betas.
    Scheduler is ReduceLROnPlateau monitoring validation loss.
    """
    in_dim = input_dim_of_dataset(args.dataset)
    class_count = num_classes_of_dataset(args.dataset)

    logic_layers = []
    arch = args.architecture
    n = args.num_neurons
    m = args.num_active
    k = args.number_kernels

    total_num_neurons = 0


    if arch == 'threshold_connected':
        llkw = dict(grad_factor=args.grad_factor)


        if args.dataset == 'mnist':
            #
            # ─── 1) CONVOLUTIONAL TREE + OR‑POOL STAGES ─────────────────────────────────
            #

            # Conv‐Tree Block #1: 1→k channels, 5×5 kernel, depth=3, no padding
            logic_layers.append(Conv(in_channels=1, out_channels=k, kernel_size=5, stride=1, padding=0))
            logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))

            # Conv‐Tree Block #2: k→2k channels, 3×3, depth=3, padding=1
            logic_layers.append(Conv(in_channels=k, out_channels=3*k, kernel_size=3, stride=1, padding=1))
            logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))

            # Conv‐Tree Block #3: 2k→4k channels, 3×3, depth=3, padding=1
            logic_layers.append(Conv(in_channels=3*k, out_channels=9*k, kernel_size=3, stride=1, padding=1))
            logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))


            logic_layers.append(torch.nn.Flatten())

            #
            # ─── 2) MLP LAYERS ─────────────────────────────────
            #

            logic_layers.append(ThresholdLayer(in_dim=1296, out_dim=n, num_active =m, **llkw))
            logic_layers.append(CustomSigmoid())
            logic_layers.append(ThresholdLayer(in_dim=n, out_dim=1000, num_active =m, **llkw))
            logic_layers.append(CustomSigmoid2())

        elif args.dataset == 'cifar10':
            #
            # ─── 1) CONVOLUTIONAL TREE + OR‑POOL STAGES ─────────────────────────────────
            #

            # Conv‐Tree Block #1: 1→k channels, 3×3 kernel, depth=3, padding=1
            logic_layers.append(Conv(in_channels=2, out_channels=k, kernel_size=3, stride=1, padding=1))
            logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))

            # Conv‐Tree Block #2: k→2k channels, 3×3, depth=3, padding=1
            logic_layers.append(Conv(in_channels=k, out_channels=4*k, kernel_size=3, stride=1, padding=1))
            logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))

            # Conv‐Tree Block #3: 2k→4k channels, 3×3, depth=3, padding=1
            logic_layers.append(Conv(in_channels=4*k, out_channels=16*k, kernel_size=3, stride=1, padding=1))
            logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))

            # Conv‐Tree Block #3: 2k→4k channels, 3×3, depth=3, padding=1
            logic_layers.append(Conv(in_channels=16*k, out_channels=32*k, kernel_size=3, stride=1, padding=1))
            logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))


            logic_layers.append(torch.nn.Flatten())

            #
            # ─── 2) MLP LAYERS ─────────────────────────────────
            #

            logic_layers.append(ThresholdLayer(in_dim=4096, out_dim=n, num_active =m, **llkw))
            logic_layers.append(CustomSigmoid2())
        
        model = torch.nn.Sequential(*logic_layers, GroupSum(class_count, args.tau))
        model = CustomModel(model)

    else:
        raise NotImplementedError(arch)


    def count_parameters(model):
        """
        Count the number of trainable parameters in the model.
        Args: model (torch.nn.Module): The model to count parameters for.
        Returns: Prints the number of parameters
        """
        return sum(p.numel() for p in model.parameters() if p.requires_grad)


    if arch == 'threshold_connected':
        # Count only ThresholdLayer parameters (skip BinaryStep)
        threshold_layers = [layer for layer in logic_layers if isinstance(layer, ThresholdLayer)]
        total_num_neurons = sum(layer.num_neurons for layer in threshold_layers)
        print(f'total_num_neurons={total_num_neurons}')
        total_num_weights = count_parameters(model)
        print(f'total_num_weights={total_num_weights}')

    """def print_trainable_parameters(model):
        for module_name, module in model.named_modules():
            params = list(module.named_parameters(recurse=False))
            if params: 
                print(f"\nLayer: '{module_name or 'root'}'")
                for param_name, param in params:
                    if param.requires_grad:
                        print(f"  {param_name}: shape {tuple(param.shape)}, count {param.numel()}")"""
                    
            
    model = model.to(device)
    print(model)
    #print_trainable_parameters(model)

    loss_fn = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, betas=(0.75, 0.90))
    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=5, threshold = 0.001, min_lr=1e-6)

    return model, loss_fn, optimizer, scheduler



def train(model, x, y, loss_fn, optimizer, device):
    """
    Perform a single training step: forward pass, loss computation, backward pass, and optimizer step.
    Returns the computed loss value.
    Parameters:
    - model: The neural network model to train.
    - x: Input data batch.
    - y: Target labels for the input data.
    - loss_fn: Loss function to compute the training loss.
    - optimizer: Optimizer to update the model parameters.
    """
    model.train(True)  # ensure training behavior
    x = x.to(device, non_blocking=True)
    y = y.to(device, non_blocking=True)

    optimizer.zero_grad(set_to_none=True)
    y_out = model(x)
    loss = loss_fn(y_out, y)
    loss.backward()

    optimizer.step()
    return float(loss.detach())


def eval(model, loader, mode):
    """
    Evaluate the model on the provided data loader in either training or evaluation mode.
    Parameters:
    - model: The neural network model to evaluate.
    - loader: DataLoader providing the evaluation data.
    - mode: Boolean indicating whether to set the model to training mode (True) or evaluation mode (False).
    Returns:
    - res: The accuracy of the model on the provided data.
    """
    orig_mode = model.training
    with torch.no_grad():
        model.train(mode=mode)
        res = np.mean(
            [
                (model(x.to(BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]).to(device)).argmax(-1) == y.to(device)).to(torch.float32).mean().item()
                for x, y in loader
            ]
        )
        model.train(mode=orig_mode)
    return res.item()


if __name__ == '__main__':  
    """
    Main function to set up and run the training and evaluation of the model based on command-line arguments.
    It handles argument parsing, dataset loading, model creation, training loop, and final evaluation.
    """ 
    parser = argparse.ArgumentParser(description='Train logic gate network on the various datasets.')

    parser.add_argument('-eid', '--experiment_id', type=int, default=None)

    parser.add_argument('--dataset', type=str, choices=[
        'mnist',
        'cifar10',
        'fashionMNIST'
    ], required=True, help='the dataset to use')
    parser.add_argument('--tau', '-t', type=float, default=10, help='the softmax temperature tau')
    parser.add_argument('--seed', '-s', type=int, default=0, help='seed (default: 0)')
    parser.add_argument('--batch-size', '-bs', type=int, default=128, help='batch size (default: 128)')
    parser.add_argument('--learning-rate', '-lr', type=float, default=0.02, help='learning rate (default: 0.01)')
    parser.add_argument('--training-bit-count', '-c', type=int, default=32, help='training bit count (default: 32)')

    parser.add_argument('--implementation', type=str, default=device, choices=[device, 'python'],
                        help='`cuda` is the fast CUDA implementation and `python` is simpler but much slower '
                        'implementation intended for helping with the understanding.')

    parser.add_argument('--num-iterations', '-ni', type=int, default=100_000, help='Number of iterations (default: 100_000)')
    parser.add_argument('--eval-freq', '-ef', type=int, default=2_000, help='Evaluation frequency (default: 2_000)')

    parser.add_argument('--valid-set-size', '-vss', type=float, default=0., help='Fraction of the train set used for validation (default: 0.)')
    parser.add_argument('--extensive-eval', action='store_true', help='Additional evaluation (incl. valid set eval).')

    parser.add_argument('--connections', type=str, default='unique', choices=['random', 'unique'])
    parser.add_argument('--architecture', '-a', type=str, default='fully_connected')
    parser.add_argument('--num_neurons', '-n', type=int)
    parser.add_argument('--num_layers', '-l', type=int)
    parser.add_argument('--num_active', '-m', type=int)
    parser.add_argument('--number_kernels', '-k', type=int, default=16, help='Number of kernels in the first convolutional layer (default: 16)')

    parser.add_argument('--grad-factor', type=float, default=1.)

    args = parser.parse_args()
    print(vars(args))

    assert args.num_iterations % args.eval_freq == 0, (
        f'iteration count ({args.num_iterations}) has to be divisible by evaluation frequency ({args.eval_freq})'
    )

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)

    train_loader, validation_loader, test_loader = load_dataset(args)
    model, loss_fn, optim, scheduler = get_model(args)


    # ─── Training loop ───────────────────────────────────────────────────────────────
    best_acc = 0 
    cumulative_loss = 0 
    j=0
    prev_avg_loss = None
    threshold=0.01
    
    # Lists to record the evolution of s from CustomSigmoid.
    for i, (x, y) in tqdm(
            enumerate(load_n(train_loader, args.num_iterations)),
            desc='iteration',
            total=args.num_iterations,
    ):
        x = x.to(BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]).to(device)
        y = y.to(device)

        loss = train(model, x, y, loss_fn, optim, device)

        # Accumulate loss and increment iteration counter for averaging
        cumulative_loss+=loss
        j+=1
            
        if (i+1) % args.eval_freq == 0:

            _ = model(x)

            # Calculate the average loss over the evaluation window
            avg_loss = cumulative_loss / j  # Compute average loss
            print(f"Iteration {i+1}/{args.num_iterations}: Loss = {avg_loss:.6f}")

            # Reset the counters for the next evaluation window
            cumulative_loss = 0
            j = 0           

            # Check if the loss increased beyond the threshold
            if prev_avg_loss is not None and avg_loss - prev_avg_loss > threshold:
                print(f"Loss increased by {avg_loss - prev_avg_loss:.6f} (threshold: {threshold}). Adjusting learning rate.")

            # Update the previous loss tracker
            prev_avg_loss = avg_loss

            # Optional: Evaluate accuracy if required
            if args.extensive_eval:
                train_accuracy_train_mode = eval(model, train_loader, mode=True)
                valid_accuracy_eval_mode = eval(model, validation_loader, mode=False)
                valid_accuracy_train_mode = eval(model, validation_loader, mode=True)
            else:
                train_accuracy_train_mode = -1
                valid_accuracy_eval_mode = -1
                valid_accuracy_train_mode = -1
            train_accuracy_eval_mode = eval(model, train_loader, mode=False)
            test_accuracy_eval_mode = eval(model, test_loader, mode=False)
            print("Batches per epoch:        ", len(test_loader))
            test_accuracy_train_mode = eval(model, test_loader, mode=True)

            r = {
                'train_acc_eval_mode': train_accuracy_eval_mode,
                'test_acc_eval_mode': test_accuracy_eval_mode,
                'test_acc_train_mode': test_accuracy_train_mode,
            }

            print(r)
            print("-----------------------------------------------------------------------------------------------------------------------------------------------------------")


            if valid_accuracy_eval_mode == -1:
                scheduler.step(avg_loss)

                # Log the current learning rate
                current_lr = optim.param_groups[0]['lr']
                print(f"Current Learning Rate: {current_lr:.2e}", flush=True)
                if test_accuracy_eval_mode > best_acc:
                    best_acc = test_accuracy_eval_mode
                    print(f"Best Accuracy: {(best_acc * 100):.2f}%", flush=True)
                        
                

    # ─── Final evaluation on the test set ─────────────────────────────────────────────
    correct, total = 0, 0
    correct_indices   = []
    incorrect_indices = []
    global_index      = 0

    with torch.no_grad():
        model.eval()

        # 1) Save the trained weights
        weights_dir = "weights"
        os.makedirs(weights_dir, exist_ok=True)
        filename = os.path.join(weights_dir, f"model_params_{args.dataset}.pth")
        torch.save(model.state_dict(), filename)

        # run the logic extraction pipeline
        original_csv, processed_csv, truth_table_csv, minimized_txt = run_full_logic_pipeline(
        dataset_name=args.dataset, ckpt_path=filename,         # NOT model_weights_path
        weights_dir=weights_dir, out_dir="out", do_stats=True,)
        

        # 2) Iterate in small batches over test_loader
        for data, labels in test_loader:
            # Move to device
            data   = data.to(device)
            labels = labels.to(device)

            # Forward + predict
            output      = model(data, verbose=True)
            predictions = output.argmax(dim=-1)

            # Count correct
            correct_batch = (predictions == labels)
            correct      += correct_batch.sum().item()
            total        += labels.size(0)

            # Record indices if you need them
            batch_size = labels.size(0)
            for i in range(batch_size):
                if correct_batch[i]:
                    correct_indices.append(global_index + i)
                else:
                    incorrect_indices.append(global_index + i)
            global_index += batch_size

    # 3) Sanity check: did we see all test samples?
    dataset_size = len(test_loader.dataset)
    print(f"Processed {total} samples out of {dataset_size} available test samples.")
    
    # 3) Compute & print overall accuracy
    test_acc = 100.0 * correct / total
    print(f"Test Accuracy: {test_acc:.2f}%")
    
