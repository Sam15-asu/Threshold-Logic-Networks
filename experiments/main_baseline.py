import argparse
import math
import random

import numpy as np
import pandas as pd
import torch
import itertools
#torch.set_default_dtype(torch.float16)
#torch.set_default_tensor_type(torch.cuda.HalfTensor)
import torchvision
from torchvision import datasets, transforms
from tqdm import tqdm
import csv
import matplotlib.pyplot as plt
import os
import torch.nn.utils.prune as prune
import openml
from sklearn.model_selection import train_test_split
#from mpl_toolkits.mplot3d import Axes3D

from results_json import ResultsJSON
from torch.optim.lr_scheduler import ReduceLROnPlateau

import mnist_dataset
import uci_datasets
from difflogic.threshold import ThresholdLayer, BinaryStep, CustomSigmoid, CustomSigmoid2, GroupSum
import difflogic.binarization as bin
#from mainn import InferenceDebugConfig
# 1) Quick local date & time
from datetime import datetime
print(datetime.now())  # e.g., 2025-10-03 17:12:45.123456


torch.set_num_threads(1)
torch.set_printoptions(threshold=float('inf'), linewidth=200)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
print("device:", device)
print("torch:", torch.__version__, "| cuda available:", torch.cuda.is_available(), "| built-with CUDA:", torch.version.cuda)
if torch.cuda.is_available():
    print("GPU:", torch.cuda.get_device_name(0))

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
    pin_memory = torch.cuda.is_available()
    validation_loader = None
    transform = transforms.Compose([transforms.ToTensor(), transforms.Lambda(lambda x: torch.flatten(x))])
    if args.dataset == 'adult':
        train_set = uci_datasets.AdultDataset('./data-uci', split='train', download=True, with_val=False)
        test_set = uci_datasets.AdultDataset('./data-uci', split='test', with_val=False)
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=int(1e6), shuffle=False)
    elif args.dataset == 'breast_cancer':
        train_set = uci_datasets.BreastCancerDataset('./data-uci', split='train', download=True, with_val=False)
        test_set = uci_datasets.BreastCancerDataset('./data-uci', split='test', with_val=False)
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=int(1e6), shuffle=False)
    elif args.dataset.startswith('monk'):
        style = int(args.dataset[4])
        train_set = uci_datasets.MONKsDataset('./data-uci', style, split='train', download=True, with_val=False)
        test_set = uci_datasets.MONKsDataset('./data-uci', style, split='test', with_val=False)
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=int(1e6), shuffle=False)

    
    elif args.dataset in ['mnnist', 'mnnist20x20']:
        train_set = mnist_dataset.MNIST('./data-mnist', train=True, download=True, remove_border=args.dataset == 'mnist20x20')
        test_set = mnist_dataset.MNIST('./data-mnist', train=False, remove_border=args.dataset == 'mnist20x20')

        train_set_size = math.ceil((1 - args.valid_set_size) * len(train_set))
        valid_set_size = len(train_set) - train_set_size
        train_set, validation_set = torch.utils.data.random_split(train_set, [train_set_size, valid_set_size])

        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True, pin_memory=True, drop_last=True, num_workers=4)
        validation_loader = torch.utils.data.DataLoader(validation_set, batch_size=args.batch_size, shuffle=False, pin_memory=True, drop_last=True)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=args.batch_size, shuffle=False, pin_memory=True, drop_last=True) 
    

    elif 'cifar-10' in args.dataset:
        transformer = {
            'cifar-10-real-input': lambda x: x,
            'cifar-10-3-thresholds': lambda x: torch.cat([(x > (i + 1) / 4).float() for i in range(3)], dim=0),
            'cifar-10-10-thresholds': lambda x: torch.cat([(x > (i + 1) / 11).float() for i in range(10)], dim=0),
        }[args.dataset]
        transformers = torchvision.transforms.Compose([
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Lambda(transformer),
        ])
        train_set = torchvision.datasets.CIFAR10('./data-cifar', train=True, download=True, transform=transformers)
        test_set = torchvision.datasets.CIFAR10('./data-cifar', train=False, transform=transformers)

        train_set_size = math.ceil((1 - args.valid_set_size) * len(train_set))
        valid_set_size = len(train_set) - train_set_size
        train_set, validation_set = torch.utils.data.random_split(train_set, [train_set_size, valid_set_size])

        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True, pin_memory=True, drop_last=True, num_workers=2)
        validation_loader = torch.utils.data.DataLoader(validation_set, batch_size=args.batch_size, shuffle=False, pin_memory=True, drop_last=True)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=args.batch_size, shuffle=False, pin_memory=True, drop_last=True)
    
    elif 'cifar10' in args.dataset:
        train_dataset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform)
        test_dataset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform)

        train_loader_bin = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=len(train_dataset), shuffle=True)
        test_loader_bin = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=len(test_dataset), shuffle=False)

        x_train, y_train = next(iter(train_loader_bin))
        x_test, y_test = next(iter(test_loader_bin))

        # Binarize with distributive thermometer
        thermometer = bin.DistributiveThermometer(10).fit(x_train)
        x_train = thermometer.binarize(x_train).flatten(start_dim=1)
        x_test = thermometer.binarize(x_test).flatten(start_dim=1)
        # Create new datasets
        train_dataset_bin = torch.utils.data.TensorDataset(x_train, y_train)
        test_dataset_bin = torch.utils.data.TensorDataset(x_test, y_test)

        # Create DataLoaders with your desired batch sizes
        train_loader = torch.utils.data.DataLoader(train_dataset_bin, batch_size=args.batch_size, shuffle=True,pin_memory=True, drop_last=True, num_workers=4)
        test_loader = torch.utils.data.DataLoader(test_dataset_bin, batch_size=args.batch_size, shuffle=False, pin_memory=True, drop_last=True)

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
        train_loader = torch.utils.data.DataLoader(train_dataset_bin, batch_size=args.batch_size, shuffle=True,pin_memory=True, drop_last=True, num_workers=4)
        test_loader = torch.utils.data.DataLoader(test_dataset_bin, batch_size=args.batch_size, shuffle=False, pin_memory=True, drop_last=True)

    elif 'mnist' in args.dataset:
        train_dataset = datasets.MNIST(root='./data', train=True, download=True, transform=transform)
        test_dataset = datasets.MNIST(root='./data', train=False, download=True, transform=transform)

        train_loader_bin = torch.utils.data.DataLoader(dataset=train_dataset, batch_size=len(train_dataset), shuffle=True)
        test_loader_bin = torch.utils.data.DataLoader(dataset=test_dataset, batch_size=len(test_dataset), shuffle=False)

        x_train, y_train = next(iter(train_loader_bin))
        x_test, y_test = next(iter(test_loader_bin))

        # Binarize with distributive thermometer
        thermometer = bin.DistributiveThermometer(3).fit(x_train)
        x_train = thermometer.binarize(x_train).flatten(start_dim=1)
        x_test = thermometer.binarize(x_test).flatten(start_dim=1)
        # Create new datasets
        train_dataset_bin = torch.utils.data.TensorDataset(x_train, y_train)
        test_dataset_bin = torch.utils.data.TensorDataset(x_test, y_test)

        # Create DataLoaders with your desired batch sizes
        train_loader = torch.utils.data.DataLoader(train_dataset_bin, batch_size=args.batch_size, shuffle=True,pin_memory=pin_memory, drop_last=True, num_workers=4)
        test_loader = torch.utils.data.DataLoader(test_dataset_bin, batch_size=args.batch_size, shuffle=False, pin_memory=pin_memory, drop_last=True)


    elif 'jsc' in args.dataset:
        # Load dataset from OpenML
        dataset = openml.datasets.get_dataset(42468)
        df_features, df_labels, _, attribute_names = dataset.get_data(dataset_format='dataframe', target=dataset.default_target_attribute)
        features = df_features.values.astype(np.float32)
        label_names = list(df_labels.unique())
        labels = np.array(df_labels.map(lambda x: label_names.index(x)).values)
        num_output = labels.max() + 1

        # Split data
        x_train, x_test, y_train, y_test = train_test_split(features, labels, train_size=0.8, random_state=42)

        # Binarization: fit thermometer on training features
        thermometer = bin.DistributiveThermometer(200).fit(x_train)
        x_train_bin = thermometer.binarize(x_train).flatten(start_dim=1)
        x_test_bin = thermometer.binarize(x_test).flatten(start_dim=1)

        # Convert numpy arrays to torch tensors
        x_train_tensor = torch.tensor(x_train_bin)
        y_train_tensor = torch.tensor(y_train, dtype=torch.int64)
        x_test_tensor = torch.tensor(x_test_bin)
        y_test_tensor = torch.tensor(y_test, dtype=torch.int64)

        # Create TensorDataset instances
        train_set = torch.utils.data.TensorDataset(x_train_tensor, y_train_tensor)
        test_set = torch.utils.data.TensorDataset(x_test_tensor, y_test_tensor)

        # Create DataLoaders
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=int(1e6), shuffle=False)

    elif 'sylvine' in args.dataset:
        # Load dataset from OpenML
        dataset = openml.datasets.get_dataset(41146)
        df_features, df_labels, _, attribute_names = dataset.get_data(dataset_format='dataframe', target=dataset.default_target_attribute)
        features = df_features.values.astype(np.float32)
        label_names = list(df_labels.unique())
        labels = np.array(df_labels.map(lambda x: label_names.index(x)).values)
        num_output = labels.max() + 1

        # Split data
        x_train, x_test, y_train, y_test = train_test_split(features, labels, train_size=0.8, random_state=42)

        # Binarization: fit thermometer on training features
        thermometer = bin.DistributiveThermometer(200).fit(x_train)
        x_train_bin = thermometer.binarize(x_train).flatten(start_dim=1)
        x_test_bin = thermometer.binarize(x_test).flatten(start_dim=1)

        # Convert numpy arrays to torch tensors
        x_train_tensor = torch.tensor(x_train_bin)
        y_train_tensor = torch.tensor(y_train, dtype=torch.int64)
        x_test_tensor = torch.tensor(x_test_bin)
        y_test_tensor = torch.tensor(y_test, dtype=torch.int64)

        # Create TensorDataset instances
        train_set = torch.utils.data.TensorDataset(x_train_tensor, y_train_tensor)
        test_set = torch.utils.data.TensorDataset(x_test_tensor, y_test_tensor)

        # Create DataLoaders
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=int(1e6), shuffle=False)


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
        'adult': 116,
        'breast_cancer': 51,
        'monk1': 17,
        'monk2': 17,
        'monk3': 17,
        'mnist': 2352,
        'mnist20x20': 400,
        'cifar-10-real-input': 3 * 32 * 32,
        'cifar-10-3-thresholds': 3 * 32 * 32 * 3,
        'cifar-10-10-thresholds': 3 * 32 * 32 * 10,
        'cifar10': 3 * 32 * 32 * 10,
        'fashionMNIST': 5488,
        'jsc': 3200,
        'sylvine': 4000
    }[dataset]


def num_classes_of_dataset(dataset):
    return {
        'adult': 2,
        'breast_cancer': 2,
        'monk1': 2,
        'monk2': 2,
        'monk3': 2,
        'mnist': 10,
        'mnist20x20': 10,
        'cifar-10-real-input': 10,
        'cifar-10-3-thresholds': 10,
        'cifar-10-10-thresholds': 10,
        'cifar10': 10,
        'fashionMNIST': 10,
        'jsc': 5,
        'sylvine': 2
    }[dataset]

def get_model(args):
    in_dim = input_dim_of_dataset(args.dataset)
    class_count = num_classes_of_dataset(args.dataset)

    layers = []
    logic_layers = []
    arch = args.architecture
    k = args.num_neurons
    l = args.num_layers
    m = args.num_active

    total_num_neurons = 0

 ########################################################################################################################

    if arch == 'fully_connected':
        layers.append(torch.nn.Flatten())
        layers.append(torch.nn.Linear(in_dim, k, dtype=BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]))
        layers.append(BinaryStep())
        layers.append(torch.nn.Linear(k, k, dtype=BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]))
        layers.append(BinaryStep())
        layers.append(torch.nn.Linear(k, 400, dtype=BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]))
        layers.append(BinaryStep())
        total_num_neurons += k
        """for _ in range(l - 2):
            p = k/4
            layers.append(torch.nn.Linear(k, p, dtype=BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]))
            k=k/4
            layers.append(CustomSigmoid(layer_id = 2))
            total_num_neurons += k"""

        layers.append(torch.nn.Linear(400, class_count, dtype=BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]))
        total_num_neurons += 10
        model = torch.nn.Sequential(*layers)
        model = CustomModel(model)


    elif arch == 'threshold_connected':
        llkw = dict(grad_factor=args.grad_factor)
        logic_layers.append(torch.nn.Flatten())
        logic_layers.append(ThresholdLayer(in_dim=in_dim, out_dim=k, layer_id=1, num_active =m, **llkw))
        logic_layers.append(CustomSigmoid(layer_id = 2))
        logic_layers.append(ThresholdLayer(in_dim=k, out_dim=1000, layer_id=3, num_active =m, **llkw))
        logic_layers.append(CustomSigmoid2())

        
        for idx in range(2, l):
            logic_layers.append(CustomSigmoid(layer_id = idx))
            
            logic_layers.append(ThresholdLayer(in_dim=k, out_dim=k,layer_id = idx, num_active =m, **llkw))  
            logic_layers.append(CustomSigmoid())
        
        model = torch.nn.Sequential(*logic_layers, GroupSum(class_count, args.tau))
        model = CustomModel(model)

    ####################################################################################################################

    else:
        raise NotImplementedError(arch)

    ####################################################################################################################

    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    if arch == 'fully_connected':
        print(f'total_num_neurons={total_num_neurons}')
        total_num_weights = count_parameters(model)
        print(f'total_num_weights={total_num_weights}')
        if args.experiment_id is not None:
            results.store_results({
                'total_num_neurons': total_num_neurons,
                'total_num_weights': total_num_weights,
            })

    elif arch == 'threshold_connected':
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
    print("model is on:", next(model.parameters()).device)

    print(model)
    print_trainable_parameters(model)

    if args.experiment_id is not None:
        results.store_results({'model_str': str(model)})

    loss_fn = torch.nn.CrossEntropyLoss()
    #loss_fn = torch.nn.BCEWithLogitsLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, betas=(0.75, 0.90))

    scheduler = ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=5, threshold = 0.001, min_lr=1e-6)

    return model, loss_fn, optimizer, scheduler


def train(model, x, y, loss_fn, optimizer):
    y_out = model(x)
    loss = loss_fn(y_out, y)
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

    return loss.item()


def eval(model, loader, mode):
    orig_mode = model.training
    with torch.no_grad():
        model.train(mode=mode)
        res = np.mean(
            [
                (model(x.to(BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count])
                       .to(device)).argmax(-1) == y.to(device)).to(torch.float32).mean().item()
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
        'adult', 'breast_cancer',
        'monk1', 'monk2', 'monk3',
        'mnist', 'mnist20x20',
        'cifar-10-real-input',
        'cifar-10-3-thresholds',
        'cifar-10-10-thresholds',
        'cifar10',
        'fashionMNIST',
        'jsc',
        'sylvine'
    ], required=True, help='the dataset to use')
    parser.add_argument('--tau', '-t', type=float, default=10, help='the softmax temperature tau')
    parser.add_argument('--seed', '-s', type=int, default=0, help='seed (default: 0)')
    parser.add_argument('--batch-size', '-bs', type=int, default=128, help='batch size (default: 128)')
    parser.add_argument('--learning-rate', '-lr', type=float, default=0.01, help='learning rate (default: 0.01)')
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
    parser.add_argument('--num_neurons', '-k', type=int)
    parser.add_argument('--num_layers', '-l', type=int)
    parser.add_argument('--num_active', '-m', type=int)

    parser.add_argument('--grad-factor', type=float, default=2.)

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

    # ——— DEBUG: inspect one batch ———
    x_batch, y_batch = next(iter(train_loader))
    print("x_batch.shape:", x_batch.shape)          # should be (batch_size, 1, 28, 28)
    print("y_batch.shape:", y_batch.shape)          # (batch_size,)
    #print("Unique values in x_batch:", torch.unique(x_batch))
    print("Unique values in y_batch:", torch.unique(y_batch))
    #print("First sample top-left 2×2:\n", x_batch[0,0,:25,:25])
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
        x = x.to(BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]).to(device, non_blocking=True)
        y = y.to(device, non_blocking = True) 

        loss = train(model, x, y, loss_fn, optim)

        # Accumulate loss and increment iteration counter for averaging
        cumulative_loss+=loss
        j+=1

        #if (i+1) % args.eval_freq == args.eval_freq - 1:
            
        if (i+1) % args.eval_freq == 0:
            for module in model.modules():
                if isinstance(module, (ThresholdLayer, CustomSigmoid, CustomSigmoid2, GroupSum)):
                    module.debug = True

            # Run a forward pass on a sample input (using the current batch 'x')
            _ = model(x)

                     

            # Turn debug off again for the relevant modules.
            for module in model.modules():
                if isinstance(module, (ThresholdLayer, CustomSigmoid, CustomSigmoid2, GroupSum)):
                    module.debug = False

            

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
            #train_accuracy_train_mode = eval(model, train_loader, mode=True)   
            train_accuracy_eval_mode = eval(model, train_loader, mode=False)
            test_accuracy_eval_mode = eval(model, test_loader, mode=False)
            test_accuracy_train_mode = eval(model, test_loader, mode=True)

            r = {
                #'train_acc_train_mode': train_accuracy_train_mode,
                'train_acc_eval_mode': train_accuracy_eval_mode,
                'test_acc_eval_mode': test_accuracy_eval_mode,
                'test_acc_train_mode': test_accuracy_train_mode,
            }

            if args.experiment_id is not None:
                results.store_results(r)
            print(r)
            print("-----------------------------------------------------------------------------------------------------------------------------------------------------------")


            if valid_accuracy_eval_mode == -1:
                scheduler.step(train_accuracy_eval_mode)

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


    # Evaluate the model on the test set.
    correct, total = 0, 0
    correct_indices = []    # List to store indices of correctly classified samples.
    incorrect_indices = []  # List to store indices of misclassified samples.
    global_index = 0        # Counter for the sample index in the entire test set.

    with torch.no_grad():
        model.eval()
        # Save the model parameters for reference.
        weights_dir = "weights"
        os.makedirs(weights_dir, exist_ok=True)
        filename = os.path.join(weights_dir, f"model_params_{args.dataset}.pth")
        torch.save(model.state_dict(), filename)
        
        # Use a DataLoader to iterate over the test dataset in one (or more) batches.
        for (data, labels) in torch.utils.data.DataLoader(test_loader.dataset, batch_size=int(1e6), shuffle=False):
            # Flatten the input data and move it (and labels) to the appropriate device.
            data = torch.nn.Flatten()(data).to(device)
            labels = labels.to(device)
            
            # Forward pass through the model.
            output = model(data, verbose=True)
            predictions = output.argmax(dim=-1)
            
            # Compute the number of correct predictions in the batch.
            correct_batch = (predictions == labels)
            correct += correct_batch.float().sum().item()
            total += data.size(0)
            
            # Record sample indices for correct and incorrect predictions.
            batch_size = data.size(0)
            for i in range(batch_size):
                if correct_batch[i].item():
                    correct_indices.append(global_index + i)
                else:
                    incorrect_indices.append(global_index + i)
                    
            global_index += batch_size

    # Calculate and print the final accuracy.
    acc3 = (correct / total) * 100
    print(f'Test Accuracy: {acc3:.2f}%')

# Load the state dict from the file.
state_dict = torch.load(filename, map_location='cpu')

# Create a new dictionary to store effective weights.
effective_state_dict = {}

# Create a CSV filename based on the pth filename.
csv_filename = os.path.join(weights_dir, f"model_params_{args.dataset}.csv")

# Iterate through the state dict keys.
for key, tensor in state_dict.items():
    # If the key corresponds to weights and a mask exists.
    if key.endswith("weight"):
        # Assume the corresponding mask is stored under a key with 'mask' instead of 'weight'
        mask_key = key.replace("weight", "mask")
        if mask_key in state_dict:
            # Compute the effective weight.
            effective_weight = tensor * state_dict[mask_key]
            # Use a new key to denote effective weights.
            effective_state_dict[key.replace("weight", "effective_weight")] = effective_weight
        else:
            # For layers without a mask, keep the weight as is.
            effective_state_dict[key] = tensor
    else:
        # For non-weight parameters (e.g., bias, mask), you might choose to store or skip them.
        effective_state_dict[key] = tensor

# Create the CSV filename for effective parameters.
csv_filename = os.path.join(weights_dir, f"model_effective_params_{args.dataset}.csv")

with open(csv_filename, 'w', newline='') as csvfile:
    writer = csv.writer(csvfile)
    # Write header: add separate columns for w1, w2, w3, w4, w5, w6 instead of one non_zero_values column.
    writer.writerow(['parameter', 'row_index', 'row_shape', 'non_zero_indices', 'w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'bias'])
    
    # Iterate over effective_state_dict keys for weights.
    # We consider keys ending with "weight" or "effective_weight".
    for name, param in effective_state_dict.items():
        # Skip keys containing ".s" or ".mask"
        if '.s' in name or '.mask' in name:
            continue

        if name.endswith("weight") or name.endswith("effective_weight"):
            weight_np = param.detach().cpu().numpy()
            
            # Determine the corresponding bias key based on the key name.
            if name.endswith("effective_weight"):
                bias_key = name.replace("effective_weight", "bias")
            else:
                bias_key = name.replace("weight", "bias")
            
            bias_tensor = effective_state_dict.get(bias_key, None)
            bias_np = bias_tensor.detach().cpu().numpy() if bias_tensor is not None else None

            # Process the weight rows.
            if weight_np.ndim == 1:
                # For 1D tensors, treat the entire tensor as one row.
                row = weight_np
                nonzero_indices = np.nonzero(row)[0]
                nonzero_values = row[nonzero_indices]
                # Convert nonzero_values to a list of strings.
                w_vals = [str(val) for val in nonzero_values]
                # Ensure there are 5 columns (pad if necessary).
                while len(w_vals) < 6:
                    w_vals.append("")
                # For a 1D bias, the corresponding element is bias_np[0]
                bias_value = bias_np[0] if (bias_np is not None and bias_np.size > 0) else ""
                writer.writerow([
                    name, 
                    0, 
                    row.shape, 
                    ','.join(map(str, nonzero_indices))
                ] + w_vals[:6] + [bias_value])
            else:
                # For a 2D matrix (e.g., 256 x 116)
                for i, row in enumerate(weight_np):
                    if row.ndim > 1:
                        row = row.flatten()
                    nonzero_indices = np.nonzero(row)[0]
                    nonzero_values = row[nonzero_indices]
                    w_vals = [str(val) for val in nonzero_values]
                    while len(w_vals) < 6:
                        w_vals.append("")
                    bias_value = bias_np[i] if (bias_np is not None and i < bias_np.shape[0]) else ""
                    writer.writerow([
                        name, 
                        i, 
                        row.shape, 
                        ','.join(map(str, nonzero_indices))
                    ] + w_vals[:6] + [bias_value])


# Load the CSV file.
df = pd.read_csv(csv_filename)

# Group the rows by the parameter name.
grouped = df.groupby('parameter')

# Iterate through each parameter group.
for param_name, group in grouped:
    all_weight_values = []
    all_bias_values = []
    
    # Loop over each row in the group.
    for idx, row in group.iterrows():
        # Instead of parsing one "non_zero_values" column, loop through columns w1 to w6.
        for col in ['w1', 'w2', 'w3', 'w4', 'w5', 'w6']:
            if pd.notna(row[col]) and row[col] != "":
                try:
                    all_weight_values.append(float(row[col]))
                except ValueError:
                    pass  # Skip if conversion fails.
        
        # Parse the bias value if available.
        if pd.notna(row['bias']) and row['bias'] != "":
            try:
                bias_val = float(row['bias'])
                all_bias_values.append(bias_val)
            except ValueError:
                pass  # Skip if conversion fails.
    
    # Compute overall weight stats.
    all_weight_values = np.array(all_weight_values)
    if all_weight_values.size > 0:
        weight_min = all_weight_values.min()
        weight_max = all_weight_values.max()
        num_weight_elements = all_weight_values.size
    else:
        weight_min, weight_max, num_weight_elements = None, None, 0

    # Compute overall bias stats.
    all_bias_values = np.array(all_bias_values)
    if all_bias_values.size > 0:
        bias_min = all_bias_values.min()
        bias_max = all_bias_values.max()
        num_bias_elements = all_bias_values.size
    else:
        bias_min, bias_max, num_bias_elements = None, None, 0

    # Print the overall stats for this parameter group.
    #print(f"{param_name} overall weights: min={weight_min}, max={weight_max}, num_elements={num_weight_elements}")
    #print(f"{param_name} overall bias:   min={bias_min}, max={bias_max}, num_elements={num_bias_elements}")


# --- CSV Processing Pipeline Functions ---

def process_input_csv(input_csv, output_csv):
    import pandas as pd

    df = pd.read_csv(input_csv)

    # Filter out rows with "model.2.weight" or "model.5.weight"
    df["parameter"] = df["parameter"].astype(str)
    df = df[~df["parameter"].str.contains(r"model\.2\.weight|model\.5\.weight", na=False)]

    # ---- IMPORTANT: sanitize and force dtypes up-front ----
    if "non_zero_indices" not in df.columns:
        df["non_zero_indices"] = ""
    # make sure it's object so we can assign strings later
    df["non_zero_indices"] = df["non_zero_indices"].fillna("").astype(str).astype("object")

    if "bias" not in df.columns:
        df["bias"] = 0.0
    df["bias"] = pd.to_numeric(df["bias"], errors="coerce").fillna(0.0)

    for i in range(1, 7):
        col = f"w{i}"
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Step 2: Add "negative index" as scalar strings
    def add_negative_index(row):
        indices = [s.strip() for s in str(row["non_zero_indices"]).split(",") if s.strip()]
        negative_indices = []
        for i in range(1, 7):
            w = row.get(f"w{i}", 0.0)
            w = 0.0 if pd.isna(w) else float(w)
            if w < 0 and (i - 1) < len(indices):
                negative_indices.append(indices[i - 1])
        return ", ".join(negative_indices)

    df["negative index"] = df.apply(add_negative_index, axis=1).astype("object")

    # Step 3: Fold negative weights into bias
    def update_bias(row):
        bias_val = float(row["bias"])
        for i in range(1, 7):
            w = row.get(f"w{i}", 0.0)
            w = 0.0 if pd.isna(w) else float(w)
            if w < 0:
                bias_val += -w  # add absolute value
        return bias_val

    df["bias"] = df.apply(update_bias, axis=1)

    # Step 4: Make all weights non-negative
    def convert_weights_to_abs(row):
        for i in range(1, 7):
            col = f"w{i}"
            w = row.get(col, None)
            if pd.isna(w):
                continue
            row[col] = abs(float(w))
        return row

    df = df.apply(convert_weights_to_abs, axis=1)

    # --- Step 5: Reorder by descending weight WITHOUT mutating the row in apply ---

    # make sure the target columns are object dtype so string assignment is safe
    df["non_zero_indices"] = df["non_zero_indices"].astype("object")
    df["negative index"] = df["negative index"].astype("object")

    def reorder_weights_indices_row(row):
        # parse indices and the set of originally-negative indices
        indices = [s.strip() for s in str(row["non_zero_indices"]).split(",") if s.strip()]
        originally_negative = {s.strip() for s in str(row["negative index"]).split(",") if s.strip()}

        # gather (weight, index, was_negative)
        pairs = []
        for i in range(1, 7):
            w = pd.to_numeric(row.get(f"w{i}", 0.0), errors="coerce")
            w = 0.0 if pd.isna(w) else float(w)
            idx = indices[i - 1] if i - 1 < len(indices) else ""
            was_neg = idx in originally_negative
            pairs.append((w, idx, was_neg))

        # sort by descending weight
        pairs.sort(key=lambda x: x[0], reverse=True)

        # prepare outputs WITHOUT mutating the input row
        new_weights = [p[0] for p in pairs]
        new_nzi = ", ".join(p[1] for p in pairs if p[1])  # keep only non-empty
        new_neg = ", ".join(p[1] for p in pairs if p[2] and p[1])

        # return as a Series for vectorized assignment after apply
        out = {f"w{i+1}": new_weights[i] for i in range(6)}
        out["non_zero_indices_new"] = new_nzi
        out["negative index_new"] = new_neg
        return pd.Series(out)

    # run once, then assign back to df in one go (no dtype fights)
    _out = df.apply(reorder_weights_indices_row, axis=1)

    for i in range(1, 7):
        df[f"w{i}"] = pd.to_numeric(_out[f"w{i}"], errors="coerce").fillna(0.0)

    df["non_zero_indices"] = _out["non_zero_indices_new"].astype("object")
    df["negative index"] = _out["negative index_new"].astype("object")


    df.to_csv(output_csv, index=False)
    return output_csv



def generate_truth_tables(input_csv, output_truth_csv):
    import itertools
    import pandas as pd

    def generate_truth_table_dict_normal(w1, w2, w3, w4, w5, w6, bias):
        truth = {}
        for combo in itertools.product([0, 1], repeat=6):
            x1, x2, x3, x4, x5, x6 = combo
            f = w1*x1 + w2*x2 + w3*x3 + w4*x4 + w5*x5 + w6*x6
            truth["".join(str(b) for b in combo)] = 1 if f >= bias else 0
        return truth

    df = pd.read_csv(input_csv)

    functions_dict = {}

    for _, row in df.iterrows():
        try:
            w1 = float(row["w1"]); w2 = float(row["w2"]); w3 = float(row["w3"])
            w4 = float(row["w4"]); w5 = float(row["w5"]); w6 = float(row["w6"])
            bias = float(row["bias"])
        except Exception:
            continue

        try:
            neuron = int(row["row_index"])
        except Exception:
            continue

        param = str(row.get("parameter", ""))
        if "model.1.effective_weight" in param:
            layer = 1
        elif ("model.3.effective_weight" in param) or ("model.4.effective_weight" in param):
            layer = 2
        else:
            continue

        truth_table = generate_truth_table_dict_normal(w1, w2, w3, w4, w5, w6, bias)
        functions_dict[f"row{neuron}_{layer}"] = truth_table

    # ---- Guard: nothing matched -> write empty CSV and exit cleanly ----
    if not functions_dict:
        pd.DataFrame(columns=["Function", "neuron number"]).to_csv(output_truth_csv, index=False)
        return output_truth_csv

    # Build header from the first entry
    first_truth = next(iter(functions_dict.values()))
    minterms = sorted(list(first_truth.keys()))

    rows = []
    for idx, (func, truth) in enumerate(functions_dict.items()):
        row_out = {"Function": func, "neuron number": f"neuron{idx}"}
        row_out.update({m: truth[m] for m in minterms})
        rows.append(row_out)

    pd.DataFrame(rows, columns=["Function", "neuron number"] + minterms).to_csv(output_truth_csv, index=False)
    return output_truth_csv



# --- Wrapping the Pipeline in a Function ---

def run_csv_pipeline(dataset_name, weights_dir="weights", out_dir="out"):
    os.makedirs(weights_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    original_csv = os.path.join(weights_dir, f"model_effective_params_{dataset_name}.csv")
    processed_csv = os.path.join(weights_dir, f"model_effective_params_{dataset_name}_changed.csv")
    truth_table_csv = os.path.join(weights_dir, f"truth_tables_generated_{dataset_name}.csv")

    process_input_csv(original_csv, processed_csv)
    generate_truth_tables(processed_csv, truth_table_csv)    

# --- End of CSV Pipeline Module ---
# Run the CSV processing pipeline for the dataset.
run_csv_pipeline(args.dataset)