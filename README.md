# 🔮 Threshold-Logic-Networks

● This repository brain main contains experiments with MLP based threshold logic networks implemented in PyTorch.

In order to train a model,

First go to the **experiments** directory :

1. cd experiments

2.  Then run this command, e.g:

        python3 experiments/main_baseline.py --dataset mnist -bs 128 -t 15 -ni 20000 -ef 1000 -l 1 -m 6 -k 2000 -a threshold_connected > results/mnist.txt

Important arguments:

--dataset : mnist, fashionMNIST, cifar10

-a --architecture : threshold_connected (uses ThresholdLayer)

-k, --num_neurons : Number of neurons in the threshold layer(s)

-l, --num_layers :  Number of threshold layers

-m, --num_active :  Number of active connections per neuron (e.g. 6)

-ni, --num-iterations : Number of training iterations (not epochs)

-ef, --eval-freq : Evaluation frequency in iterations

-t, --tau : Softmax temperature for the final GroupSum layer


● The script will train a model and dump the weight parameters, process them , generate truth table and produce a minimized boolean expression. It is located in the **/out** directory.
                            
    /out/minimized_boolean_expressions_mnist.txt


● In order to generate the reset and verilog files for the boolean expressions, go to the **/extras** directory

1. cd extras
2. Run these two commands

       python neuron.py <dataset>
       python resetfile_generator.py <dataset>

Dataset options are: mnist, fashionMNIST, cifar10
