import argparse
import random

import numpy as np
import pandas as pd
import torch
import torchvision
from torchvision import datasets, transforms
from tqdm import tqdm
import matplotlib.pyplot as plt
import os
import torch.nn.utils.prune as prune
from pipeline import run_full_logic_pipeline
from torch.optim.lr_scheduler import ReduceLROnPlateau

import mnist_dataset
from difflogic.threshold import ThresholdLayer, CustomSigmoid, CustomSigmoid2, GroupSum
import difflogic.binarization as bin

from datetime import datetime
print(datetime.now())  


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
    
    if 'cifar10' in args.dataset:
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
        'mnist': 2352,
        'cifar10': 3 * 32 * 32 * 10,
        'fashionMNIST': 5488
    }[dataset]


def num_classes_of_dataset(dataset):
    return {
        'mnist': 10,
        'cifar10': 10,
        'fashionMNIST': 10
    }[dataset]

def get_model(args):
    in_dim = input_dim_of_dataset(args.dataset)
    class_count = num_classes_of_dataset(args.dataset)

    logic_layers = []
    arch = args.architecture
    k = args.num_neurons
    l = args.num_layers
    m = args.num_active

    total_num_neurons = 0

 ########################################################################################################################


    if arch == 'threshold_connected':
        llkw = dict(grad_factor=args.grad_factor)
        logic_layers.append(torch.nn.Flatten())

        if (args.dataset == 'mnist'):
            logic_layers.append(ThresholdLayer(in_dim=in_dim, out_dim=k, layer_id=1, num_active =m, **llkw))
            logic_layers.append(CustomSigmoid(layer_id = 2))
            logic_layers.append(ThresholdLayer(in_dim=k, out_dim=1000, layer_id=3, num_active =m, **llkw))
            logic_layers.append(CustomSigmoid2())
        
        elif (args.dataset == 'cifar10'):
            logic_layers.append(ThresholdLayer(in_dim=in_dim, out_dim=k, layer_id=1, num_active =m, **llkw))
            logic_layers.append(CustomSigmoid2())
        
        model = torch.nn.Sequential(*logic_layers, GroupSum(class_count, args.tau))
        model = CustomModel(model)

    else:
        raise NotImplementedError(arch)


    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    if arch == 'threshold_connected':
        # Count only ThresholdLayer parameters (skip CustomSigmoid)
        threshold_layers = [layer for layer in logic_layers if isinstance(layer, ThresholdLayer)]
        total_num_neurons = sum(layer.num_neurons for layer in threshold_layers)
        print(f'total_num_neurons={total_num_neurons}')
        total_num_weights = count_parameters(model)
        print(f'total_num_weights={total_num_weights}')

    """
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
    """              
            
    model = model.to(device)
    print("model is on:", next(model.parameters()).device)
    print(model)
    print('\n')
    #print_trainable_parameters(model)

    loss_fn = torch.nn.CrossEntropyLoss()
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
    print(vars(args))

    assert args.num_iterations % args.eval_freq == 0, (
        f'iteration count ({args.num_iterations}) has to be divisible by evaluation frequency ({args.eval_freq})'
    )

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
        x = x.to(BITS_TO_TORCH_FLOATING_POINT_TYPE[args.training_bit_count]).to(device, non_blocking=True)
        y = y.to(device, non_blocking = True) 

        loss = train(model, x, y, loss_fn, optim)

        # Accumulate loss and increment iteration counter for averaging
        cumulative_loss+=loss
        j+=1

        #if (i+1) % args.eval_freq == args.eval_freq - 1:
            
        if (i+1) % args.eval_freq == 0:
            # Run a forward pass on a sample input (using the current batch 'x')
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
            #train_accuracy_train_mode = eval(model, train_loader, mode=True)   
            train_accuracy_eval_mode = eval(model, train_loader, mode=False)
            test_accuracy_eval_mode = eval(model, test_loader, mode=False)
            test_accuracy_train_mode = eval(model, test_loader, mode=True)

            r = {
                'train_acc_eval_mode': train_accuracy_eval_mode,
                'test_acc_eval_mode': test_accuracy_eval_mode,
            }
        
            print("-----------------------------------------------------------------------------------------------------------------------------------------------------------")
            print(r)

            if valid_accuracy_eval_mode == -1:
                scheduler.step(train_accuracy_eval_mode)

                # Log the current learning rate
                current_lr = optim.param_groups[0]['lr']
                print(f"Current Learning Rate: {current_lr:.2e}", flush=True)
                if test_accuracy_eval_mode > best_acc:
                    best_acc = test_accuracy_eval_mode
                    print(f"Best Accuracy: {best_acc * 100:.2f}%", flush=True)
                        
                    


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

        # run the logic extraction pipeline
        run_full_logic_pipeline(dataset_name=args.dataset, model_weights_path=filename, 
                                weights_dir=weights_dir, out_dir="out",)

        
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