#!/bin/bash
gpu=0
data_dir=task0-data/out
aug_dir=task0-data/aug
hall_dir=task0-data/hall
ckpt_dir=checkpoints/sigmorphon20-task0

lang=$1
arch=transformer

seed=$2

model_copy=$3

lr=0.001
scheduler=warmupinvsqr
max_steps=10000
warmup=4000
beta2=0.98
label_smooth=0.1
total_eval=10000
bs=400

# transformer
layers=4
hs=1024
embed_dim=256
nb_heads=4
dropout=${4:-0.3}


CUDA_VISIBLE_DEVICES=$gpu python src/train.py \
    --dataset sigmorphon17task1 \
    --train $hall_dir/$lang.hall \
    --dev $data_dir/$lang.dev \
    --model $ckpt_dir/$arch/$lang/hall/$model_copy/model-$lang \
    --embed_dim $embed_dim --src_hs $hs --trg_hs $hs --dropout $dropout --nb_heads $nb_heads \
    --label_smooth $label_smooth --total_eval $total_eval \
    --src_layer $layers --trg_layer $layers --max_norm 1 --lr $lr --shuffle \
    --arch $arch --gpuid 0 --estop 1e-8 --bs $bs --max_steps $max_steps \
    --scheduler $scheduler --warmup_steps $warmup --cleanup_anyway --beta2 $beta2 --bestacc --seed $seed
