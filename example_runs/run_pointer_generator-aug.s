#!/bin/bash
#SBATCH --cpus-per-task=8
#SBATCH --nodes=1
#SBATCH --gres=gpu:1
#SBATCH --time=06:30:00
#SBATCH --mem=32GB
#SBATCH --job-name=as12152
#SBATCH --mail-user=as12152@nyu.edu
#SBATCH --output=slurm_%j.out


. ~/.bashrc
module load anaconda3/4.7.12

conda activate ssl-framework

cd /scratch/as12152/code/ORIGINAL_BASELINE/neural-transducer

lang=$1
seed=$2
model_copy=$3
bash task0-pg-aug.sh $lang $seed $model_copy