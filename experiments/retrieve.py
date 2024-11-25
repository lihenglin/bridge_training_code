import os

import jax
import jax.numpy as jnp
import numpy as np
import tensorflow as tf
from absl import app, flags, logging
from flax.training import checkpoints
from ml_collections import config_flags
import scipy
import imageio

from jaxrl_m.agents import pretrain_agents
from jaxrl_m.data.bc_dataset import glob_to_path_list
from jaxrl_m.data.retrieval_dataset import RetrievalDataset
from jaxrl_m.vision import encoders, decoders
from jaxrl_m.data.text_processing import text_processors

FLAGS = flags.FLAGS

flags.DEFINE_string("checkpoint_path", None, "Path to the checkpoint to load.", required=True)
flags.DEFINE_string("target_dataset_path", None, "Path to the target dataset.", required=True)
flags.DEFINE_string("prior_dataset_path", None, "Path to the prior dataset.", required=True)
flags.DEFINE_string("prior_dataset_flow_dtype", "float32", "Data type of the flow field.")
flags.DEFINE_float("threshold", 0.1, "Threshold for retrieval.")
flags.DEFINE_string("output_dir", None, "Path to the output directory.", required=True)
flags.DEFINE_string("prefix", "", "Prefix for the output file.")
flags.DEFINE_integer("act_pred_horizon", 1, "Horizon for the agent.")
flags.DEFINE_bool("prechunk", False, "Whether the dataset is prechunked.")

config_flags.DEFINE_config_file(
    "config",
    None,
    "File path to the training hyperparameter configuration.",
    lock_config=False,
)


def tensor_feature(value):
    return tf.train.Feature(
        bytes_list=tf.train.BytesList(value=[tf.io.serialize_tensor(value).numpy()])
    )


def main(_):
    # prevent tensorflow from using GPUs
    tf.config.set_visible_devices([], "GPU")

    # load datasets
    target_paths = [glob_to_path_list(FLAGS.target_dataset_path, prefix=FLAGS.config.data_path)]
    prior_paths  = [glob_to_path_list(FLAGS.prior_dataset_path,  prefix=FLAGS.config.data_path)]

    target_paths = [[os.path.join(path, "train/out.tfrecord") for path in sub_list] for sub_list in target_paths]
    prior_paths  = [sorted([os.path.join(path, "train/out.tfrecord") for path in sub_list]) for sub_list in prior_paths]

    target_data = RetrievalDataset(
        target_paths,
        batch_size=FLAGS.config.batch_size,
        act_pred_horizon=FLAGS.act_pred_horizon,
        prechunk=False,
    )
    target_data_iter = target_data.iterator()
    target_batch = next(target_data_iter)

    # define encoder
    encoder_def = encoders[FLAGS.config.encoder](**FLAGS.config.encoder_kwargs)

    # define decoder
    decoder_def = decoders[FLAGS.config.decoder](**FLAGS.config.decoder_kwargs)

    # initialize agent
    rng = jax.random.PRNGKey(FLAGS.config.seed)
    rng, construct_rng = jax.random.split(rng)
    agent = pretrain_agents[FLAGS.config.agent].create(
        rng=construct_rng,
        observations=target_batch,
        encoder=encoder_def,
        decoder=decoder_def,
        **FLAGS.config.agent_kwargs,
    )
    agent = checkpoints.restore_checkpoint(FLAGS.checkpoint_path, target=agent)

    # compute target embeddings
    target_embeddings = []
    while True:
        target_embeddings.append(agent.compute_embeddings(target_batch))

        try:
            target_batch = next(target_data_iter)
        except StopIteration:
            break

    target_embeddings = jnp.concatenate(target_embeddings, axis=0)
    logging.info(f"target size: {target_embeddings.shape[0]}")
    logging.info("Finish computing target embeddings.")

    # compute prior embeddings
    prior_data = RetrievalDataset(
        prior_paths,
        batch_size=FLAGS.config.batch_size,
        act_pred_horizon=FLAGS.act_pred_horizon,
        prechunk=FLAGS.prechunk,
        flow_dtype=FLAGS.prior_dataset_flow_dtype,
    )

    prior_data_iter  = prior_data.iterator()
    sim_scores = []
    while True:
        try:
            prior_batch = next(prior_data_iter)
            if len(sim_scores) == 0:
                logging.info(f"Shape of actions: {prior_batch['actions'].shape}")
                logging.info(f"First three actions of the first batch: {prior_batch['actions'][:3]}")
            prior_embeddings = agent.compute_embeddings(prior_batch)
            sim_scores.append(-jnp.min(scipy.spatial.distance.cdist(target_embeddings, prior_embeddings), axis=0))
        except StopIteration:
            break
    sim_scores = jnp.concatenate(sim_scores, axis=0)

    logging.info(f"prior size: {sim_scores.shape[0]}")
    logging.info("Finish computing similarity scores.")

    # find retrieved data
    retrieval_distances = -sim_scores
    sorted_distances = np.argsort(retrieval_distances)
    threshold_idx = sorted_distances[:int(FLAGS.threshold * len(sorted_distances))]
    mask = np.zeros_like(retrieval_distances, dtype=np.bool_)
    mask[threshold_idx] = True

    # store retrieved data
    prior_data = RetrievalDataset(
        prior_paths,
        batch_size=FLAGS.config.batch_size,
        act_pred_horizon=FLAGS.act_pred_horizon,
        prechunk=FLAGS.prechunk,
        include_future_obs=True,
        flow_dtype=FLAGS.prior_dataset_flow_dtype,
    )
    prior_data_iter  = prior_data.iterator()

    cnt = 0
    current_idx = 0
    while True:
        try:
            prior_batch = next(prior_data_iter)
            current_mask = mask[current_idx:current_idx+len(prior_batch['actions'])]
            current_idx += len(prior_batch['actions'])
        except StopIteration:
            break

        if np.sum(current_mask) != 0:
            for idx in np.where(current_mask)[0]:
                imageio.imwrite(f"vis/{cnt}.png",
                                np.concatenate((
                                    prior_batch['observations']['image'][idx],
                                    prior_batch['next_observations']['image'][idx],
                                ), axis=1))
                cnt += 1

    
if __name__ == "__main__":
    app.run(main)