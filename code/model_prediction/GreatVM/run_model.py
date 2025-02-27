import sys

from tensorflow.python.autograph.utils.ag_logging import log
sys.path.append('.')

import argparse
import yaml

import tensorflow as tf
import numpy as np

from checkpoint_tracker import Tracker
from data import data_loader, vocabulary
from meta_model import VarMisuseModel

step_count=1

def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("data_path", help="Path to data root")
	ap.add_argument("vocabulary_path", help="Path to vocabulary file")
	ap.add_argument("config", help="Path to config file")
	ap.add_argument("-m", "--models", help="Directory to store trained models (optional)")
	ap.add_argument("-l", "--log", help="Path to store training log (optional)")
	ap.add_argument("-e", "--eval_only", help="Whether to run just the final model evaluation")
	args = ap.parse_args()
	config = yaml.safe_load(open(args.config))
	print("Training with configuration:", config)
	data = data_loader.DataLoader(args.data_path, config["data"], vocabulary.Vocabulary(args.vocabulary_path))
	if args.eval_only:
		if args.models is None or args.log is None:
			raise ValueError("Must provide a path to pre-trained models when running final evaluation")
		test(data, config, args.models, args.log)
	else:
		train(data, config, args.models, args.log)

def test(data, config, model_path, log_path):
	model = VarMisuseModel(config['model'], data.vocabulary.vocab_dim)
	model.run_dummy_input()
	tracker = Tracker(model, model_path, log_path)
	tracker.restore(best_model=True)
	evaluate(data, config, model, is_heldout=False)

def train(data, config, model_path=None, log_path=None):
	model = VarMisuseModel(config['model'], data.vocabulary.vocab_dim)
	model.run_dummy_input()
	print("Model initialized, training {:,} parameters".format(np.sum([np.prod(v.shape) for v in model.trainable_variables])))
	optimizer = tf.optimizers.Adam(config["training"]["learning_rate"])

	# Restore model from checkpoints if present; also sets up logger
	if model_path is None:
		tracker = Tracker(model)
	else:
		tracker = Tracker(model, model_path, log_path)
	tracker.restore()
	if tracker.ckpt.step.numpy() > 0:
		print("Restored from step:", tracker.ckpt.step.numpy() + 1)
	else:
		print("Step:", tracker.ckpt.step.numpy() + 1)
	
	mbs = 0
	losses, accs, counts = get_metrics()
	train_batcher = data.batcher(mode='train')  # Initialize training batcher outside the training loop as it is set up to continue iterating indefinitely.
	while tracker.ckpt.step < config["training"]["max_steps"]:
		step_count = tracker.ckpt.step.numpy().item() + 1
		with open(f'step_{step_count}_output.txt', 'w') as log_file:
			# These are just for console logging, not global counts
			for batch in train_batcher:
				mbs += 1
				tokens, edges, error_loc, repair_targets, repair_candidates = batch[0]
				token_mask = tf.clip_by_value(tf.reduce_sum(tokens, -1), 0, 1)
				
				# Write batch JSON data.
				write_batch(log_file, step_count, batch[1])

				with tf.GradientTape() as tape:
					pointer_preds = model(tokens, token_mask, edges, training=True)
					ls, acs, to_print = model.get_loss(pointer_preds, token_mask, error_loc, repair_targets, repair_candidates)
					loc_loss, rep_loss = ls
					loss = loc_loss + rep_loss

				grads = tape.gradient(loss, model.trainable_variables)
				grads, _ = tf.clip_by_global_norm(grads, 0.25)
				optimizer.apply_gradients(list(zip(grads, model.trainable_variables)))

				write_losses(log_file, step_count, to_print)

				# Update statistics
				num_buggy = tf.reduce_sum(tf.clip_by_value(error_loc, 0, 1))
				samples = tf.shape(token_mask)[0]
				prev_samples = tracker.get_samples()
				curr_samples = tracker.update_samples(samples)
				update_metrics(losses, accs, counts, token_mask, ls, acs, num_buggy)
			
				'''
				# Every few minibatches, print the recent training performance
				if mbs % config["training"]["print_freq"] == 0:
					avg_losses = ["{0:.3f}".format(l.result().numpy()) for l in losses]
					avg_accs = ["{0:.2%}".format(a.result().numpy()) for a in accs]
					print("MB: {0}, seqs: {1:,}, tokens: {2:,}, loss: {3}, accs: {4}".format(mbs, curr_samples, counts[1].result().numpy(), ", ".join(avg_losses), ", ".join(avg_accs)))
					[l.reset_states() for l in losses]
					[a.reset_states() for a in accs]
				'''
				
				# Every valid_interval samples, run an evaluation pass and store the most recent model with its heldout accuracy
				if prev_samples // config["data"]["valid_interval"] < curr_samples // config["data"]["valid_interval"]:
					
					# Print the training stats for this step before running evaluate
					avg_accs = [a.result().numpy() for a in accs]
					avg_accs_str = ", ".join(["{0:.2%}".format(a) for a in avg_accs])
					avg_loss_str = ", ".join(["{0:.3f}".format(l.result().numpy()) for l in losses])
					print("Training result: seqs: {0:,}, tokens: {1:,}, loss: {2}, accs: {3}".format(counts[0].result().numpy(), counts[1].result().numpy(), avg_loss_str, avg_accs_str))
					
					avg_accs = evaluate(data, config, model, log_file)
					tracker.save_checkpoint(model, avg_accs)
					step_count = tracker.ckpt.step.numpy().item() + 1
					print("Step:", tracker.ckpt.step.numpy() + 1)
					break  # Exit the current loop to start the next output file. The outer while loop will keep the training going.
	

def evaluate(data, config, model, log_file=None, is_heldout=True):  # Similar to train, just without gradient updates
	if is_heldout:
		print("Running evaluation pass on heldout data")
	else:
		print("Testing pre-trained model on full eval data")
	
	losses, accs, counts = get_metrics()
	mbs = 0
	for batch in data.batcher(mode='dev' if is_heldout else 'eval'):
		mbs += 1
		tokens, edges, error_loc, repair_targets, repair_candidates = batch[0]		
		token_mask = tf.clip_by_value(tf.reduce_sum(tokens, -1), 0, 1)

		if log_file:
			write_batch(log_file, step_count, batch[1])
		
		pointer_preds = model(tokens, token_mask, edges, training=False)
		ls, acs, to_print = model.get_loss(pointer_preds, token_mask, error_loc, repair_targets, repair_candidates)

		if log_file:
			write_losses(log_file, step_count, to_print)


		num_buggy = tf.reduce_sum(tf.clip_by_value(error_loc, 0, 1))
		update_metrics(losses, accs, counts, token_mask, ls, acs, num_buggy)
		if is_heldout and counts[0].result() > config['data']['max_valid_samples']:
			break
		if not is_heldout and mbs % config["training"]["print_freq"] == 0:
			avg_losses = ["{0:.3f}".format(l.result().numpy()) for l in losses]
			avg_accs = ["{0:.2%}".format(a.result().numpy()) for a in accs]
			print("Testing progress: MB: {0}, seqs: {1:,}, tokens: {2:,}, loss: {3}, accs: {4}".format(mbs, counts[0].result().numpy(), counts[1].result().numpy(), ", ".join(avg_losses), ", ".join(avg_accs)))

	avg_accs = [a.result().numpy() for a in accs]
	avg_accs_str = ", ".join(["{0:.2%}".format(a) for a in avg_accs])
	avg_loss_str = ", ".join(["{0:.3f}".format(l.result().numpy()) for l in losses])
	print("Evaluation result: seqs: {0:,}, tokens: {1:,}, loss: {2}, accs: {3}".format(counts[0].result().numpy(), counts[1].result().numpy(), avg_loss_str, avg_accs_str))
	return avg_accs

def get_metrics():
	losses = [tf.keras.metrics.Mean() for _ in range(2)]
	accs = [tf.keras.metrics.Mean() for _ in range(4)]
	counts = [tf.keras.metrics.Sum(dtype='int32') for _ in range(2)]
	return losses, accs, counts

def update_metrics(losses, accs, counts, token_mask, ls, acs, num_buggy_samples):
	loc_loss, rep_loss = ls
	no_bug_pred_acc, bug_loc_acc, target_loc_acc, joint_acc = acs
	num_samples = tf.shape(token_mask)[0]
	counts[0].update_state(num_samples)
	counts[1].update_state(tf.reduce_sum(token_mask))
	losses[0].update_state(loc_loss)
	losses[1].update_state(rep_loss)
	accs[0].update_state(no_bug_pred_acc, sample_weight=num_samples - num_buggy_samples)
	accs[1].update_state(bug_loc_acc, sample_weight=num_buggy_samples)
	accs[2].update_state(target_loc_acc, sample_weight=num_buggy_samples)
	accs[3].update_state(joint_acc, sample_weight=num_buggy_samples)


def write_batch(log_file, step_count, json_data):
	log_file.write("BATCH_START |Step:" + str(step_count) + "|Size: " + str(len(json_data)) + "\n")
	for json_sample in json_data:
		log_file.write(json_sample + "\n")
	log_file.write("BATCH_END\n")


def write_losses(log_file, step_count, to_print):
	for data, name in zip(to_print, ('LOC_PROB_SAMP', 'LOC_LOSS_SAMP', 'TGT_PROB_SAMP', 'TGT_LOSS_SAMP')):
		write_loss(log_file, step_count, name, data)


def write_loss(log_file, step_count, name, data):
	data_list = data.numpy().tolist()
	data_str = ' '.join([str(elem) for elem in data_list])
	log_file.write(name + "_START |Step:" + str(step_count) + "|Size: " + str(len(data)) + "\n" + data_str + "\n" )
	log_file.write(name + "_END\n")


if __name__ == '__main__':
	main()
