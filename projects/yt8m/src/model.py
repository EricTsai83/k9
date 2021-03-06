"""Video classification model on YouTube-8M dataset."""

import os
import logging
from functools import partial

import pandas as pd
import numpy as np
import tensorflow as tf
from tensorflow import feature_column as fc
from tensorflow.python.lib.io import file_io
from tensorflow.python.ops import init_ops
from .eval_metrics import AverageNClass, HitAtOne


N_CLASS = 3862
BATCH_SIZE = 1024
VOCAB_FILE = "data/vocabulary.csv"
# Exclude audio feature since we didn't implement audio feature extraction.
# Even if the model can be trained on audio feature,
# they won't be available for inference on new video.
FEAT_COL_VIDEO = [
    fc.numeric_column(key="mean_rgb", shape=(1024,), dtype=tf.float32),
    #fc.numeric_column(key="mean_audio", shape=(128,), dtype=tf.float32),
    fc.indicator_column(fc.categorical_column_with_identity(key="labels", num_buckets=N_CLASS))
]
FEAT_X = ["mean_rgb"]
FEAT_SPEC_VIDEO = fc.make_parse_example_spec(FEAT_COL_VIDEO)
MULTI_HOT_ENCODER = tf.keras.layers.DenseFeatures(FEAT_COL_VIDEO[-1])
# If we'd like to use a custom serving input function, we need to use the estimator API.
# There is no document on how a keras model can use a custom serving input function.
KERAS_TO_ESTIMATOR = True


def calc_class_weight(infile, scale=1):
    """Calculate class weight to re-balance label distribution.
    The class weight for class i (w_i) is determined by:
    w_i = total no. samples / (n_class * count(class i))
    """
    if infile.startswith("gs://"):
        with file_io.FileIO(infile, "r") as f:
            vocab = pd.read_csv(f)
    else:
        vocab = pd.read_csv(infile)
    vocab.sort_values("Index", inplace=True)
    cnt = vocab["TrainVideoCount"]
    w = cnt.sum() / (len(vocab) * cnt)
    w = w.values.astype(np.float32)
    return pow(w, scale)


def _parse(examples, spec, batch_size, n_class):
    features = tf.io.parse_example(examples, features=spec)
    labels = features.pop("labels")
    labels = MULTI_HOT_ENCODER({"labels": labels})
    return features, labels


def input_fn(infiles, spec, mode=tf.estimator.ModeKeys.TRAIN):
    dataset = tf.data.TFRecordDataset(tf.io.gfile.glob(infiles))
    if mode == tf.estimator.ModeKeys.TRAIN:
        dataset = dataset.shuffle(buffer_size=1000).repeat(count=None).batch(BATCH_SIZE, drop_remainder=True)
    else:
        dataset = dataset.batch(BATCH_SIZE, drop_remainder=False)
    dataset = dataset.map(partial(_parse, spec=spec, batch_size=BATCH_SIZE, n_class=N_CLASS))
    dataset = dataset.prefetch(BATCH_SIZE)
    return dataset


def serving_input_receiver_fn():
    """Parse seralized tfrecord string for online inference."""
    # Accept a list of serialized tfrecord string.
    example_bytestring = tf.compat.v1.placeholder(shape=[None], dtype=tf.string)
    # Parse them into feature tensors.
    features = tf.io.parse_example(example_bytestring, FEAT_SPEC_VIDEO)
    features.pop("labels")  # Dummy label. Not important at all.
    return tf.estimator.export.ServingInputReceiver(features, {"examples_bytes": example_bytestring})


class BaseModel:

    def __init__(self, params):
        self.params = params
        config = tf.estimator.RunConfig(
            tf_random_seed=777,
            save_checkpoints_steps=max(1000, params["train_steps"] // 10),
            model_dir=params["model_dir"]
        )
        self.class_weights = calc_class_weight(VOCAB_FILE, scale=1)
        self.serving_input_receiver_fn = serving_input_receiver_fn
        if KERAS_TO_ESTIMATOR:
            self.estimator = tf.keras.estimator.model_to_estimator(keras_model=self.model_fn(), config=config)
        else:
            self.estimator = self.model_fn()

    def model_fn(self):

        def hamming_loss(y_true, y_pred):
            loss = tf.keras.backend.binary_crossentropy(y_true, y_pred, from_logits=False)
            if self.params["weighted_loss"]:
                loss *= self.class_weights
            return tf.reduce_mean(tf.reduce_sum(loss, axis=1))

        FEAT_COL_X = [col for col in FEAT_COL_VIDEO if col.name in FEAT_X]
        l2_reg = tf.keras.regularizers.l2(1e-8)
        if KERAS_TO_ESTIMATOR:
            # DenseFeatures doesn't play well with Estimator.
            inputs = tf.keras.layers.Input(shape=(1024,), name="mean_rgb")
            predictions = tf.keras.layers.Dense(N_CLASS, activation="sigmoid", kernel_regularizer=l2_reg)(inputs)
            model = tf.keras.Model(inputs=inputs, outputs=predictions, name="baseline")
        else :
            model = tf.keras.models.Sequential(name="baseline")
            model.add(tf.keras.layers.DenseFeatures(FEAT_COL_X, name="mean_rgb"))
            model.add(tf.keras.layers.Dense(N_CLASS, activation="sigmoid", kernel_regularizer=l2_reg))
        model.compile(
            optimizer="adam",
            loss=hamming_loss,
            metrics=[
                tf.keras.metrics.Precision(),
                tf.keras.metrics.Recall(),
                AverageNClass(),
                HitAtOne()
            ]
        )
        return model

    def train_and_evaluate(self, params):
        if KERAS_TO_ESTIMATOR:
            # This is much slower than the native keras model fit under TF 2.0.
            train_spec = tf.estimator.TrainSpec(
                input_fn=lambda: input_fn(params["train_data_path"], spec=FEAT_SPEC_VIDEO),
                max_steps=params["train_steps"]
            )
            exporter = tf.estimator.FinalExporter(
                name="exporter", serving_input_receiver_fn=serving_input_receiver_fn)
            eval_spec = tf.estimator.EvalSpec(
                input_fn=lambda: input_fn(params["eval_data_path"], spec=FEAT_SPEC_VIDEO,
                                          mode=tf.estimator.ModeKeys.EVAL),
                steps=100,
                start_delay_secs=60,
                throttle_secs=1,
                exporters=exporter
            )
            logging.getLogger("tensorflow").setLevel(logging.INFO)
            tf.estimator.train_and_evaluate(
                estimator=self.estimator,
                train_spec=train_spec,
                eval_spec=eval_spec
            )
        else:
            model_dir = os.path.join(".", params["model_dir"])
            train_dataset = input_fn(params["train_data_path"], spec=FEAT_SPEC_VIDEO)
            valid_dataset = input_fn(params["eval_data_path"], spec=FEAT_SPEC_VIDEO,
                                     mode=tf.estimator.ModeKeys.EVAL)
            tb_callback = tf.keras.callbacks.TensorBoard(log_dir=model_dir)
            self.estimator.fit(
                train_dataset, validation_data=valid_dataset,
                epochs=params["train_epochs"], steps_per_epoch=params["train_steps"],
                validation_steps=100,
                callbacks=[tb_callback])
            self.estimator.save(model_dir, save_format="tf")