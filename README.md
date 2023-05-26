# DL2-GVP
- Luka Wong Chung
- Wouter Haringhuizen
- Erencan Tatar

Read the report on [our Blogpost](./Blogpost.md).


-----

<code to run files and jobs >  .job files

    
    ## Introduction

This repo uses the original Geometric Vector Perceptrons [GVP](https://github.com/drorlab/gvp-pytorch/tree/main), a rotation-equivariant GNN, and combines a TransformerConv layer attenmpting to further improve the the baseline model.
Scripts for training / testing / sampling on protein design and training / testing on all ATOM3D tasks are provided.
Trained model are provided in the foler "TODO".
We provide a blogpost.md and GVP.ipynb walking through all the experiments.

## Table Of Content
- [Installation](#installation)
    - [Requirements](#composer)
    - [Environment](#Environment)
    - [Download Atom3D dataset](#download-dataset)
- [Run experiments](#Run-experiments)
    - [aa](#database-setup)
- [Links](#links)

## Installation
For general usage and deeper understand of individual files we refer back to the original readme [GVP](https://github.com/drorlab/gvp-pytorch/tree/main)
### Requirements


```bash
conda create -n gvp
source activate gvp

conda install pytorch torchvision torchaudio pytorch-cuda=11.7 -c pytorch -c nvidia
conda install pyg -c pyg
conda install pytorch-scatter -c pyg
conda install pytorch-cluster -c pyg
conda install pytorch-sparse -c pyg
conda install tqdm numpy scikit-learn
pip install atom3d  
```
***Note***: The original atom3d module might contain a error regarding 'get_subunits' and 'get_negatives' from the subpackage `neighbors`

### Environment
In order to test a correct install run  `test_equivariance.py`

### Download-dataset
In order to download the ATOM3d datasets with splits we use the `download_atomdataset.job`. Running this file downloads the necesarry datasets by running `download_atom3d.py` and saves them into the right directories.

## Run-experiments
### Training
In order to train the model on the ATOM3D datasets, we used the `run_atom3d.job` file in which we called `run_atom3d.py` for the different datasets and on different seeds. The usage of `run_atom3d.py` is as follows:
```
 $ python run_atom3d.py -h

usage: run_atom3d.py [-h] [--num-workers N] [--smp-idx IDX]
                     [--lba-split SPLIT] [--batch SIZE] [--train-time MINUTES]
                     [--val-time MINUTES] [--epochs N] [--test PATH]
                     [--lr RATE] [--load PATH]
                     TASK

positional arguments:
  TASK                  {RES, MSP, SMP, LBA, LEP}

```
Here is an example of what the `run_atom3d.job` file would look like to train the models on the LEP dataset and task with different seeds:
```
#!/bin/bash

#SBATCH --partition=gpu_titanrtx_shared_course
#SBATCH --gres=gpu:1
#SBATCH --job-name=RunAtom3D
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=3
#SBATCH --time=36:00:00
#SBATCH --mem=32000M
#SBATCH --output=../job_logs/slurm_output_%A.out

module purge
module load 2022
module load Anaconda3/2022.05

source activate gvp

srun python run_atom3d.py LEP --batch 2 --seed 0 
srun python run_atom3d.py LEP --batch 2 --seed 34
srun python run_atom3d.py LEP --batch 2 --seed 42

srun python run_atom3d.py LEP --batch 2 --seed 0 --transformer
srun python run_atom3d.py LEP --batch 2 --seed 34 --transformer
srun python run_atom3d.py LEP --batch 2 --seed 42 --transformer
```
This saves each model at every epoch to their specific directory (i.e. /models/LEP/Transformer/32/LEP_0000000000.0000000_00_TF.pt). In the output file from this job you can find at what epoch the model with the lowest validation loss was saved, which can be used for the evaluation.
    
### Testing
To evaluate a model, simply use the `--test` argument with `run_atom3d.py` and give the path to the model that you want to evaluate. See below an example of what this would look like for a model trained with TransformerConv on the LEP task with seed 42:
```
python run_atom3d.py LEP --test models/LEP/Transformer/42/LEP_0000000000.0000000_00_TF.pt --seed 42 --batch 2 --transformer
```
    
## Links




* [Web site](https://aimeos.org/integrations/typo3-shop-extension/)
* [Documentation](https://aimeos.org/docs/TYPO3)
* [Forum](https://aimeos.org/help/typo3-extension-f16/)
* [Issue tracker](https://github.com/aimeos/aimeos-typo3/issues)
* [Source code](https://github.com/aimeos/aimeos-typo3)
