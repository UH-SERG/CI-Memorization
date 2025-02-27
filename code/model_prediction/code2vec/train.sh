#!/usr/bin/env bash
###########################################################
# Change the following values to train a new model.
# type: the name of the new model, only affects the saved file name.
# dataset: the name of the dataset, as was preprocessed using preprocess.sh
# test_data: by default, points to the validation set, since this is the set that
#   will be evaluated after each training iteration. If you wish to test
#   on the final (held-out) test set, change 'val' to 'test'.

mDb="$1"  # e.g., java-small
mPt="$2"  # e.g., n_percent
type=${mDb}-model
dataset_name=${mDb}
data_dir=data/${mPt}/${mDb}
data=${data_dir}/${dataset_name}
test_data="./data/0_percent/${mDb}/${mDb}.test.c2v"
model_dir=models/${mPt}/${type}

mkdir -p ${model_dir}
set -e
python3 -u code2vec.py --data ${data} --test ${test_data} --save ${model_dir}/saved_model
