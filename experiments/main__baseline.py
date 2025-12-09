import argparse
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+

now_phx = datetime.now(ZoneInfo("America/Phoenix"))
print(now_phx.strftime("%Y-%m-%d %H:%M:%S %Z"))

import numpy as np
import pandas as pd
import torch
import itertools
#torch.set_default_dtype(torch.float16)
#torch.set_default_tensor_type(torch.cuda.HalfTensor)
import torchvision
from torchvision import datasets, transforms
from tqdm import tqdm
import torch.nn as nn
import csv
import matplotlib.pyplot as plt
import os
os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
import torch.nn.utils.prune as prune
import openml
from sklearn.model_selection import train_test_split
#from mpl_toolkits.mplot3d import Axes3D

from results_json import ResultsJSON
from torch.optim.lr_scheduler import ReduceLROnPlateau

import mnist_dataset
import uci_datasets
from difflogic import LogicLayer
from difflogic.threshold import ThresholdLayer, CustomSigmoid, CustomSigmoid2, GroupSum
import difflogic.binarization as bin
from difflogic.connections import Conv
#from mainn import InferenceDebugConfig

torch.set_num_threads(1)
torch.set_printoptions(threshold=float('inf'), linewidth=200)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print("device:", device)
import re

# Your desired layer tags
LAYER_MAP = {0: 1, 2: 3, 4: 5, 7: 7, 9: 9}

def get_layer_tag(param: str):
    """
    Parse 'model.<idx>.(gatebank(.w)?| (effective_)?weight )' and map to your layer tag.
    Returns None for rows you want to skip.
    """
    if not isinstance(param, str):
        return None
    m = re.search(r'model\.(\d+)\.(?:gatebank(?:\.w)?|(?:effective_)?weight)\b', param)
    if not m:
        return None
    model_idx = int(m.group(1))
    return LAYER_MAP.get(model_idx)  # None if not in map

BITS_TO_TORCH_FLOATING_POINT_TYPE = {
    16: torch.float16,
    32: torch.float32,
    64: torch.float64
}
    
class CustomModel(torch.nn.Module):
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
    validation_loader = None
    transform = transforms.Compose([transforms.ToTensor(), transforms.Lambda(lambda x: torch.flatten(x))])
    

    if 'cifar10' in args.dataset:

        # 1) Load CIFAR-10 (no flatten; values in [0,1])
        #    If you use heavy data augmentation elsewhere, fit thresholds on a plain ToTensor() view.
        transform_plain = transforms.ToTensor()  # or use your 'transform' if it's just normalization
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
        #x_bin = x_bin.permute(0, 3, 1, 2)   # → (B, 2, 28, 28)

        # 2) Move the bit‑plane axis into the channel axis
        #    Permute (batch, H, W, bits) → (batch, bits, H, W)
        #x_bin = x_bin.permute(0, 3, 1, 2)      # now shape [N, 2, 28, 28]

        # same for test
        test_loader_big = torch.utils.data.DataLoader(dataset=test_dataset,batch_size=len(test_dataset),shuffle=False)

        x_test_all, y_test_all = next(iter(test_loader_big))
        x_test_bin = thermometer.binarize(x_test_all)  # → (Ntest, 1, 28, 28, 2)
        x_test_bin = x_test_bin.squeeze(-1)             # → (Ntest, 28, 28, 2)
        #x_test_bin = x_test_bin.permute(0, 3, 1, 2)        # → (Ntest, 2, 28, 28)
    
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
    i = 0
    while i < n:
        for x in loader:
            yield x
            i += 1
            if i == n:
                break


def input_dim_of_dataset(dataset):
    return {
        'mnist': 1568,
        'mnist20x20': 400,
        'cifar-10-real-input': 3 * 32 * 32,
        'cifar-10-3-thresholds': 3 * 32 * 32 * 3,
        'cifar-10-10-thresholds': 3 * 32 * 32 * 10,
        'cifar10': 3 * 32 * 32 * 10,
        'fashionMNIST': 5488
    }[dataset]


def num_classes_of_dataset(dataset):
    return {
        'mnist': 10,
        'mnist20x20': 10,
        'cifar-10-real-input': 10,
        'cifar-10-3-thresholds': 10,
        'cifar-10-10-thresholds': 10,
        'cifar10': 10,
        'fashionMNIST': 10
    }[dataset]

def get_model(args):
    in_dim = input_dim_of_dataset(args.dataset)
    class_count = num_classes_of_dataset(args.dataset)

    logic_layers = []
    arch = args.architecture
    n = args.num_neurons
    m = args.num_active
    k = args.number_kernels

    total_num_neurons = 0

 ########################################################################################################################


    if arch == 'threshold_connected':
        llkw = dict(grad_factor=args.grad_factor)

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

        # Conv‐Tree Block #3: 2k→4k channels, 3×3, depth=3, padding=1
        #logic_layers.append(Conv(in_channels=16*k, out_channels=32*k, kernel_size=3, stride=1, padding=1))
        #logic_layers.append(nn.MaxPool2d(kernel_size=2, stride=2))


        logic_layers.append(torch.nn.Flatten())

        logic_layers.append(ThresholdLayer(in_dim=1296, out_dim=n, num_active =m, **llkw))
        logic_layers.append(CustomSigmoid())
        logic_layers.append(ThresholdLayer(in_dim=n, out_dim=1000, num_active =m, **llkw))
        logic_layers.append(CustomSigmoid2())
        
        model = torch.nn.Sequential(*logic_layers, GroupSum(class_count, args.tau))
        model = CustomModel(model)

    ####################################################################################################################

    else:
        raise NotImplementedError(arch)

    ####################################################################################################################

    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)


    if arch == 'threshold_connected':
        # Count only ThresholdLayer parameters (skip BinaryStep)
        threshold_layers = [layer for layer in logic_layers if isinstance(layer, ThresholdLayer)]
        total_num_neurons = sum(layer.num_neurons for layer in threshold_layers)
        print(f'total_num_neurons={total_num_neurons}')
        #total_num_weights = sum(layer.num_weights for layer in threshold_layers)
        total_num_weights = count_parameters(model)
        #print(f'total_num_weights={total_num_weights}')
        print(f'total_num_weights={total_num_weights}')
        if args.experiment_id is not None:
            results.store_results({
                'total_num_neurons': total_num_neurons,
                'total_num_weights': total_num_weights,
            })

    def print_trainable_parameters(model):
        print("Trainable parameters by layer:")
        for module_name, module in model.named_modules():
            # Retrieve parameters that belong only to this module (not its children)
            params = list(module.named_parameters(recurse=False))
            if params:  # if the module has any parameters registered
                print(f"\nLayer: '{module_name or 'root'}'")
                for param_name, param in params:
                    if param.requires_grad:
                        print(f"  {param_name}: shape {tuple(param.shape)}, count {param.numel()}")
                    
            
    model = model.to(device)

    print(model)
    print_trainable_parameters(model)

    if args.experiment_id is not None:
        results.store_results({'model_str': str(model)})

    loss_fn = torch.nn.CrossEntropyLoss()
    #loss_fn = torch.nn.BCEWithLogitsLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, betas=(0.75, 0.90))

    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=5, threshold = 0.001, min_lr=1e-6)

    return model, loss_fn, optimizer, scheduler



def train(model, x, y, loss_fn, optimizer, device):
    model.train(True)  # ensure training behavior
    x = x.to(device, non_blocking=True)
    y = y.to(device, non_blocking=True)

    optimizer.zero_grad(set_to_none=True)
    y_out = model(x)
    loss = loss_fn(y_out, y)
    loss.backward()

    # 🔍 gradient sanity check
    for n, p in model.named_parameters():
        if p.requires_grad and p.grad is None:
            print("NO GRAD:", n)

    optimizer.step()
    return float(loss.detach())


def eval(model, loader, mode):
    orig_mode = model.training
    with torch.no_grad():
        model.train(mode=mode)
        #print(model.CustomSigmoid.self.s)
        res = np.mean(
            [
                (model(x.to(BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]).to(device)).argmax(-1) == y.to(device)).to(torch.float32).mean().item()
                for x, y in loader
            ]
        )
        model.train(mode=orig_mode)
    return res.item()


if __name__ == '__main__':

    ####################################################################################################################
    
    parser = argparse.ArgumentParser(description='Train logic gate network on the various datasets.')

    parser.add_argument('-eid', '--experiment_id', type=int, default=None)

    parser.add_argument('--dataset', type=str, choices=[
        'mnist',
        'cifar-10-real-input',
        'cifar-10-3-thresholds',
        'cifar-10-10-thresholds',
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

    ####################################################################################################################

    print(vars(args))

    assert args.num_iterations % args.eval_freq == 0, (
        f'iteration count ({args.num_iterations}) has to be divisible by evaluation frequency ({args.eval_freq})'
    )

    if args.experiment_id is not None:
        assert 520_000 <= args.experiment_id < 530_000, args.experiment_id
        results = ResultsJSON(eid=args.experiment_id, path='./results/')
        results.store_args(args)

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)

    train_loader, validation_loader, test_loader = load_dataset(args)
    model, loss_fn, optim, scheduler = get_model(args)

    ####################################################################################################################

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
        #CURRENT_EPOCH = i
        x = x.to(BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]).to(device)
        y = y.to(device)

        #print("x.shape:", x.shape)  # should be (batch_size, 1, 28, 28)
        
        loss = train(model, x, y, loss_fn, optim, device)

        # Accumulate loss and increment iteration counter for averaging
        cumulative_loss+=loss
        j+=1

        #if (i+1) % args.eval_freq == args.eval_freq - 1:
            
        if (i+1) % args.eval_freq == 0:

            # Run a forward pass on a sample input (using the current batch 'x')
            with torch.no_grad():
                y_out = model(x)
                print(f"[Iter {i+1}] Logits:\n{y_out[:3]}")

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

            if args.experiment_id is not None:
                results.store_results(r)
            print(r)
            print("-----------------------------------------------------------------------------------------------------------------------------------------------------------")


            if valid_accuracy_eval_mode == -1:
                scheduler.step(avg_loss)

                # Log the current learning rate
                current_lr = optim.param_groups[0]['lr']
                print(f"Current Learning Rate: {current_lr:.2e}", flush=True)
                if test_accuracy_eval_mode > best_acc:
                    best_acc = test_accuracy_eval_mode
                    print(f"Best Accuracy: {best_acc * 100:.2f}%", flush=True)
                    if args.experiment_id is not None:
                        results.store_final_results(r)
                        print("Best Training Accuracy result saved")
                    else:
                        print('IS THE BEST UNTIL NOW.')
                        
            elif valid_accuracy_eval_mode > best_acc:
                best_acc = valid_accuracy_eval_mode
                if args.experiment_id is not None:
                    results.store_final_results(r)
                else:
                    print('IS THE BEST UNTIL NOW.')
                    
            if args.experiment_id is not None:
                results.save()


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
    #assert total == dataset_size, "Oops—we did not process every test sample!"


import os, re, csv
import numpy as np
import pandas as pd
import torch

# ===================== Layer mapping helper =====================
LAYER_MAP = {0: 1, 2: 3, 4: 5, 6: 7, 8: 9}

def get_layer_tag(param: str):
    """
    Parse 'model.<idx>.(gatebank(.w)?| (effective_)?weight )' and map to your layer tag.
    Returns None for rows you want to skip.
    """
    if not isinstance(param, str):
        return None
    m = re.search(r'model\.(\d+)\.(?:gatebank(?:\.w)?|(?:effective_)?weight)\b', param)
    if not m:
        return None
    model_idx = int(m.group(1))
    return LAYER_MAP.get(model_idx)

# ===================== Exporter =====================
FAN_IN_TARGET = 6  # keep 2^6 truth tables

def build_effective_state_dict(state_dict: dict) -> dict:
    """Make integer/dense layers 'effective' via mask multiply. Conv gatebank goes out as-is."""
    eff = {}
    for k, t in state_dict.items():
        if k.endswith("weight"):
            mkey = k.replace("weight", "mask")
            if mkey in state_dict:
                eff[k.replace("weight", "effective_weight")] = t * state_dict[mkey]
            else:
                eff[k] = t
        else:
            eff[k] = t
    return eff

def write_effective_params_csv(state_dict: dict, csv_filename: str):
    eff = build_effective_state_dict(state_dict)
    with open(csv_filename, 'w', newline='') as f:
        wtr = csv.writer(f)
        wtr.writerow(['parameter','row_index','row_shape','non_zero_indices','w1','w2','w3','w4','w5','w6','bias'])

        for name, param in eff.items():
            # Skip slopes/masks
            if any(tag in name for tag in ('.s', '.s_raw', '.mask')):
                continue

            # --- Conv gatebank kernels (one row per output channel) ---
            if name.endswith(".gatebank.w"):
                base = name[:-2]  # strip '.w'
                theta_key = base + ".theta"
                idx_key   = base + ".idx"
                if theta_key not in eff or idx_key not in eff:
                    continue

                W   = param.detach().cpu().numpy()                 # (out_dim, fan_in_actual)
                TH  = eff[theta_key].detach().cpu().numpy()        # (out_dim,)
                IDX = eff[idx_key].detach().cpu().numpy()          # (out_dim, fan_in_actual)
                out_dim, fan_in_actual = W.shape

                for j in range(out_dim):
                    triples = [(abs(float(wv)), float(wv), int(ix)) for wv, ix in zip(W[j], IDX[j])]
                    if fan_in_actual > FAN_IN_TARGET:
                        triples.sort(key=lambda t: t[0], reverse=True)
                        triples = triples[:FAN_IN_TARGET]
                    weights = [t[1] for t in triples]
                    indices = [t[2] for t in triples]
                    while len(weights) < FAN_IN_TARGET:
                        weights.append("")

                    wtr.writerow([
                        name,
                        j,
                        (len(indices),),                      # e.g., (6,)
                        ",".join(map(str, indices)),
                        *map(str, weights[:FAN_IN_TARGET]),
                        TH[j],
                    ])
                continue  # don't fall through to generic handler

            # --- Dense/FC (effective_)weight as sparse-6 per row ---
            if name.endswith("weight") or name.endswith("effective_weight"):
                W = param.detach().cpu().numpy()
                bkey = name.replace("effective_weight", "bias") if name.endswith("effective_weight") else name.replace("weight", "bias")
                B = eff.get(bkey, None)
                Bnp = B.detach().cpu().numpy() if B is not None else None

                if W.ndim == 1:
                    row = W
                    nz_idx = np.nonzero(row)[0]
                    nz_val = row[nz_idx]
                    w_vals = [str(v) for v in nz_val]
                    while len(w_vals) < FAN_IN_TARGET: w_vals.append("")
                    bias_val = Bnp[0] if (Bnp is not None and Bnp.size > 0) else ""
                    wtr.writerow([name, 0, row.shape, ",".join(map(str, nz_idx))] + w_vals[:FAN_IN_TARGET] + [bias_val])
                else:
                    for i, row in enumerate(W):
                        if row.ndim > 1:
                            row = row.flatten()
                        nz_idx = np.nonzero(row)[0]
                        nz_val = row[nz_idx]
                        w_vals = [str(v) for v in nz_val]
                        while len(w_vals) < FAN_IN_TARGET: w_vals.append("")
                        bias_val = Bnp[i] if (Bnp is not None and i < (Bnp.shape[0] if hasattr(Bnp, 'shape') else 0)) else ""
                        wtr.writerow([name, i, row.shape, ",".join(map(str, nz_idx))] + w_vals[:FAN_IN_TARGET] + [bias_val])

# ===================== CSV processing =====================
def process_input_csv(input_csv, output_csv):
    df = pd.read_csv(input_csv)

    # Keep only rows mapped by your layer tags
    df = df[df["parameter"].apply(lambda p: get_layer_tag(str(p)) is not None)]

    # Add "negative index"
    def add_negative_index(row):
        indices = [item.strip() for item in str(row["non_zero_indices"]).split(",")]
        neg = []
        for i in range(1, 7):
            try:
                w = float(row[f"w{i}"])
            except Exception:
                w = 0.0
            if w < 0 and i-1 < len(indices):
                neg.append(indices[i-1])
        return ", ".join(neg)
    df["negative index"] = df.apply(add_negative_index, axis=1)

    # Fold negatives into bias
    def update_bias(row):
        try:
            b = float(row["bias"])
        except Exception:
            b = 0.0
        for i in range(1, 7):
            try:
                w = float(row[f"w{i}"])
            except Exception:
                w = 0.0
            if w < 0:
                b += abs(w)
        return b
    df["bias"] = df.apply(update_bias, axis=1)

    # Make weights absolute
    def to_abs(row):
        for i in range(1, 7):
            try:
                row[f"w{i}"] = abs(float(row[f"w{i}"]))
            except Exception:
                pass
        return row
    df = df.apply(to_abs, axis=1)

    # Reorder by descending weight, keep indices & negative flags aligned
    def reorder(row):
        indices = [item.strip() for item in str(row["non_zero_indices"]).split(",")]
        neg_set = set([s.strip() for s in str(row["negative index"]).split(",") if s.strip()])
        pairs = []
        for i in range(1, 7):
            try:
                w = float(row[f"w{i}"])
            except Exception:
                w = 0.0
            idx = indices[i-1] if i-1 < len(indices) else ""
            pairs.append((w, idx, idx in neg_set))
        pairs.sort(key=lambda x: x[0], reverse=True)
        for i, (w, _, _) in enumerate(pairs, start=1):
            row[f"w{i}"] = w
        row["non_zero_indices"] = ", ".join(idx for (_, idx, _) in pairs)
        row["negative index"] = ", ".join(idx for (_, idx, f) in pairs if f)
        return row
    df = df.apply(reorder, axis=1)

    df.to_csv(output_csv, index=False)
    return output_csv

# ===================== Truth table generation =====================
def generate_truth_tables(input_csv, output_truth_csv):
    import itertools

    # 2^6 minterms for fan-in = 6
    minterms = [''.join(bits) for bits in itertools.product('01', repeat=6)]

    df = pd.read_csv(input_csv)

    # Coerce numeric fields
    for col in ['w1','w2','w3','w4','w5','w6','bias']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    rows = []
    counter = 0
    for _, r in df.iterrows():
        param = str(r.get('parameter', ''))
        layer = get_layer_tag(param)
        if layer is None:
            continue

        try:
            w = [float(r[f"w{i}"]) for i in range(1, 7)]
            b = float(r["bias"])
        except Exception:
            continue

        # Build truth table: 1 if sum(w_i * x_i) >= bias else 0
        truth = {}
        for combo in itertools.product([0,1], repeat=6):
            s = sum(wi * xi for wi, xi in zip(w, combo))
            truth[''.join(map(str, combo))] = 1 if s >= b else 0

        func_label = f"row{int(r['row_index'])}_{layer}"
        row_data = {"Function": func_label, "neuron number": f"neuron{counter}"}
        row_data.update({m: truth[m] for m in minterms})
        rows.append(row_data)
        counter += 1

    if not rows:
        raise ValueError("generate_truth_tables: no rows produced. Check parameter names and layer mapping.")

    truth_df = pd.DataFrame(rows, columns=["Function","neuron number"] + minterms)
    truth_df.to_csv(output_truth_csv, index=False)
    return output_truth_csv

# ===================== Pipeline wrapper =====================
def run_csv_pipeline(dataset_name, ckpt_path, weights_dir="weights", out_dir="out", do_stats=False):
    os.makedirs(weights_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    # 1) export effective params (conv gatebank + FC) to CSV
    original_csv = os.path.join(weights_dir, f"model_effective_params_{dataset_name}.csv")
    state_dict = torch.load(ckpt_path, map_location='cpu')
    write_effective_params_csv(state_dict, original_csv)

    # (optional) quick stats
    if do_stats:
        df = pd.read_csv(original_csv)
        wcols = ['w1','w2','w3','w4','w5','w6']
        for c in wcols + ['bias']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        w_stats = (
            df.melt(id_vars=['parameter'], value_vars=wcols, value_name='w')
              .dropna(subset=['w'])
              .groupby('parameter')['w']
              .agg(weight_min='min', weight_max='max', num_weight_elements='count')
        )
        b_stats = (
            df[['parameter','bias']].dropna(subset=['bias'])
              .groupby('parameter')['bias']
              .agg(bias_min='min', bias_max='max', num_bias_elements='count')
        )
        stats = w_stats.join(b_stats, how='outer').fillna({'num_weight_elements':0,'num_bias_elements':0})
        print(stats.head())

    # 2) process → generate
    processed_csv   = os.path.join(weights_dir, f"model_effective_params_{dataset_name}_changed.csv")
    truth_table_csv = os.path.join(weights_dir, f"truth_tables_generated_{dataset_name}.csv")

    process_input_csv(original_csv, processed_csv)
    generate_truth_tables(processed_csv, truth_table_csv)

    return original_csv, processed_csv, truth_table_csv


# e.g., args.dataset is 'fashionMNIST'; args.ckpt is your .pth path
original_csv, processed_csv, truth_table_csv = run_csv_pipeline(args.dataset, filename, do_stats=True)

