#!/bin/bash

model_copy=1
seed=0
while IFS= read -r line; do
    line_arr=( $line )
    lang=${line_arr[0]}
    echo "Running pretrained transformer aug: $lang $seed $model_copy"
    sbatch run_transformer-aug-from_pretrain.s $lang $seed $model_copy
done < SIG_2020-lang-patience-hall_pretrain.txt