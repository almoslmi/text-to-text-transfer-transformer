# Copyright 2019 The T5 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utilities for data loading and processing.

Defines Tasks, TaskRegistry, Mixture, and MixtureRegistry
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc
import json
import os
import re

from absl import logging
import gin
import numpy as np
from t5.data import sentencepiece_vocabulary
import tensorflow.compat.v1 as tf
import tensorflow_datasets as tfds

_DEFAULT_FEATURE_KEYS = ["inputs", "targets"]

_VALID_TASK_NAME_REGEX = re.compile(r"^[\w\d\._]+$")
_INFO_FILENAME = "info.{split}.json"
_STATS_FILENAME = "stats.{split}.json"
_TFRECORD_PREFIX = "{split}.tfrecord"
_MAX_EXAMPLES_TO_MEM_CACHE = 1000
_SHUFFLE_BUFFER_SIZE = 1000

_TFDS_DATA_DIR_OVERRIDE = None
_GLOBAL_CACHE_DIRECTORIES = []


def set_tfds_data_dir_override(tfds_data_dir):
  global _TFDS_DATA_DIR_OVERRIDE
  _TFDS_DATA_DIR_OVERRIDE = tfds_data_dir


def set_global_cache_dirs(global_cache_dirs):
  global _GLOBAL_CACHE_DIRECTORIES
  _GLOBAL_CACHE_DIRECTORIES = global_cache_dirs


def add_global_cache_dirs(global_cache_dirs):
  global _GLOBAL_CACHE_DIRECTORIES
  _GLOBAL_CACHE_DIRECTORIES += global_cache_dirs


class DatasetProviderBase(object):
  """Abstract base for classes that provide a tf.data.Dataset."""

  __metaclass__ = abc.ABCMeta

  @abc.abstractproperty
  def sentencepiece_model_path(self):
    raise NotImplementedError

  @abc.abstractproperty
  def output_features(self):
    raise NotImplementedError

  @abc.abstractmethod
  def get_vocabulary(self):
    raise NotImplementedError

  @abc.abstractmethod
  def get_dataset(
      self, sequence_length, split, use_cached=True, shuffle=True):
    raise NotImplementedError


class DatasetProviderRegistry(object):
  """Base for registry of data providers.

  Child classes must implement a _REGISTRY dict.
  """

  _PROVIDER_TYPE = DatasetProviderBase

  @classmethod
  def add(cls, name, provider_cls, *provider_args, **provider_kwargs):
    """Adds provider to the registry."""
    if name in cls._REGISTRY:
      raise ValueError("Attempting to register duplicate provider: %s" % name)
    provider = provider_cls(*provider_args, **provider_kwargs)
    if not isinstance(provider, cls._PROVIDER_TYPE):
      raise ValueError(
          "Attempting to register a class not of an invalid type. "
          "Expecting instance of %s, got %s" %
          (cls._PROVIDER_TYPE, provider_cls))

    cls._REGISTRY[name] = provider

  @classmethod
  def get(cls, name):
    """Returns provider from the registry."""
    if name not in cls._REGISTRY:
      raise ValueError("Provider name not registered: %s" % name)
    return cls._REGISTRY[name]

  @classmethod
  def names(cls):
    """Returns all provider names in registry."""
    return cls._REGISTRY.keys()

  @classmethod
  def get_dataset(
      cls, name, sequence_length, split, use_cached=True, shuffle=True):
    return cls.get(name).get_dataset(
        sequence_length=sequence_length, split=split, use_cached=use_cached,
        shuffle=shuffle)


class LazyTfdsLoader(object):
  """Wrapper for TFDS datasets with memoization and additional functionality.

  Lazily loads info from TFDS and provides memoization to avoid expensive hidden
  file operations. Also provides additional utility methods.
  """

  _MEMOIZED_INSTANCES = {}

  def __new__(cls, name, data_dir=None):
    """Either creates a new dataset or returns it if it already exists."""
    key = (name, data_dir)
    if key not in cls._MEMOIZED_INSTANCES:
      cls._MEMOIZED_INSTANCES[key] = object.__new__(cls)
    return cls._MEMOIZED_INSTANCES[key]

  def __init__(self, name, data_dir=None):
    """LazyTfdsLoader constructor.

    Args:
      name: str, the name of the TFDS dataset.
      data_dir: str (optional), directory to read/write TFDS data.
    """
    self._name = name
    self._data_dir = data_dir
    self._builder = None

  def __getstate__(self):
    """Remove un-pickle-able attributes and return the state."""
    state = self.__dict__.copy()
    del state["_builder"]
    return state

  def __getnewargs__(self):
    return self._name, self._data_dir

  @property
  def name(self):
    return self._name

  @property
  def data_dir(self):
    if _TFDS_DATA_DIR_OVERRIDE:
      if self._data_dir:
        logging.warning(
            "Overriding TFDS data directory '%s' with '%s' for dataset '%s'.",
            self._data_dir, _TFDS_DATA_DIR_OVERRIDE, self.name)
      return _TFDS_DATA_DIR_OVERRIDE
    return self._data_dir

  @property
  def builder(self):
    if not self._builder:
      self._builder = tfds.builder(self.name, data_dir=self.data_dir)
    return self._builder

  @property
  def info(self):
    return self.builder.info

  def files(self, split):
    """Returns set containing paths to TFDS TFRecord files for the dataset."""
    self.verify_split(split)
    files = set()

    def _get_builder_files(builder):
      split_info = builder.info.splits[split]
      if builder.version.implements(tfds.core.Experiment.S3):
        num_shards = len(split_info.shard_lengths)
      else:
        num_shards = split_info.num_shards
      return tfds.core.naming.filepaths_for_dataset_split(
          dataset_name=builder.name,
          split=split_info.name,
          num_shards=num_shards,
          data_dir=builder._data_dir,  # pylint:disable=protected-access
          filetype_suffix="tfrecord",
      )

    if self.builder.BUILDER_CONFIGS and "/" not in self.name:
      # If builder has multiple configs, and no particular config was
      # requested, then compute all.
      for config in self.builder.BUILDER_CONFIGS:
        builder_for_config = tfds.builder(self.builder.name, config=config)
        files.update(_get_builder_files(builder_for_config))
    else:
      files.update(_get_builder_files(self.builder))

    if not files:
      logging.fatal("No TFRecord files found for dataset: %s", self.name)
    return files

  def load(self, split, shuffle_files):
    """Returns a tf.data.Dataset for the given split."""
    self.verify_split(split)
    return tfds.load(
        self._name,
        split=split,
        data_dir=self.data_dir,
        shuffle_files=shuffle_files,
        download=True,
        try_gcs=True)

  def load_shard(self, shard_path):
    """Returns a dataset for a single shard of the TFDS TFRecord files."""
    ds = tfds.core.file_format_adapter.TFRecordExampleAdapter(
        self.info.features.get_serialized_info()).dataset_from_filename(
            shard_path)
    ds = ds.map(self.info.features.decode_example)
    return ds

  def verify_split(self, split):
    """Verify that `split` is a valid split."""
    if split not in self.info.splits.keys():
      raise ValueError("{} has no '{}' split".format(self.name, split))

  def size(self, split):
    """Returns the number of examples in the split."""
    self.verify_split(split)
    ds_splits = self.info.splits
    dataset_size = ds_splits[split].num_examples
    # Very large datasets have num_examples = 0; default instead to np.inf
    dataset_size = dataset_size if dataset_size > 0 else np.inf
    return dataset_size


def encode_string_features(
    dataset, vocabulary, keys, copy_plaintext=False):
  """Encode specified string features.

  Passes through non-string features unchanged. Optionally passes through copy
  of original string features with "_plaintext" suffix added to the key.

  Args:
    dataset: a tf.data.Dataset
    vocabulary: a vocabulary.Vocabulary
    keys: list of strings, keys of features to encode.
    copy_plaintext: bool, whether to pass through copies of plaintext strings
      with a "_plaintext" suffix added to the key.
  Returns:
    a tf.data.Dataset
  """
  keys = set(keys)
  def my_fn(features):
    """Encode all specified feature that are strings and return a dictionary.

    Args:
      features: a dictionary
    Returns:
      a dictionary
    """
    ret = {}
    for k, v in features.items():
      if v.dtype == tf.string and k in keys:
        if copy_plaintext:
          ret["%s_plaintext" % k] = v
        v = tf.cast(vocabulary.encode_tf(v), tf.int64)
      ret[k] = v
    return ret
  return dataset.map(my_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE)


def dict_to_tfexample(ex):
  """Convert example dictionary to tf.train.Example proto."""
  feature_dict = {}
  for k, v in ex.items():
    t = tf.constant(v)
    if len(t.shape) == 0:  # pylint:disable=g-explicit-length-test
      v = [v]
    elif len(t.shape) == 1:
      v = list(v)
    else:
      raise ValueError(
          "Unsupported shape (%s) for '%s' value: %s" %
          (tf.shape, k, v))

    if t.dtype == tf.string and len(t.shape) <= 1:
      feature_dict[k] = tf.train.Feature(
          bytes_list=tf.train.BytesList(
              value=[tf.compat.as_bytes(t) for t in v]))
    elif t.dtype in (tf.int32, tf.int64) and len(t.shape) <= 1:
      feature_dict[k] = tf.train.Feature(
          int64_list=tf.train.Int64List(value=v))
    else:
      raise ValueError(
          "Unsupported type (%s) and shape (%s) for '%s' value: %s" %
          (tf.dtype, tf.shape, k, v))

  return tf.train.Example(features=tf.train.Features(feature=feature_dict))


def inverse_dataset(dataset, label):
  """Invert examples and prepend the given label to the new inputs.

  Args:
    dataset: tf.data.Dataset, contains "inputs" and "targets" keys
    label: str, the label to prepend to the inputs.
  Returns:
    a tf.data.Dataset
  """
  def map_fn(x):
    return {
        "inputs": tf.strings.join([label, x["targets"]]),
        "targets": x["inputs"],
    }
  return dataset.map(
      map_fn, num_parallel_calls=tf.data.experimental.AUTOTUNE)


# ================================ Tasks =======================================
def get_info_path(data_dir, split):
  return os.path.join(data_dir, _INFO_FILENAME.format(split=split))


def get_tfrecord_prefix(data_dir, split):
  return os.path.join(data_dir, _TFRECORD_PREFIX.format(split=split))


def get_stats_path(data_dir, split):
  return os.path.join(data_dir, _STATS_FILENAME.format(split=split))


class Task(DatasetProviderBase):
  """A wrapper for a TFDS dataset along with preprocessing information.

  Tasks handle preprocessing (via arbitrary TF function) and tokenization
  (via SentencePiece). Non-train splits also pass through the original
  plaintext strings with a "_plaintext" suffix added to the key.

  """

  def __init__(self,
               name,
               tfds_name,
               text_preprocessor,
               sentencepiece_model_path,
               metric_fns,
               postprocess_fn=None,
               token_preprocessor=None,
               tfds_data_dir=None,
               output_features=None,
               splits=None):
    """Task constructor.

    Args:
      name: string, a unique name for the Task. A ValueError will be raised if
        another task with this name is already registered.
      tfds_name: string, the name and version number of a TFDS dataset,
        optionally with a config.
      text_preprocessor: a function (or list of functions) that (each) takes in
        a tf.data.Dataset of string features and returns a tf.data.Dataset of
        string features. Can be set to None as a no-op. If a list is given,
        they will be executed sequentially.
      sentencepiece_model_path: string, path to a SentencePiece model file to
        use for tokenization.
      metric_fns: list(callable), list of metric functions with the signature
        metric_fn(targets, predictions) to use during evaluation.
      postprocess_fn: function, a function that takes in decoded model outputs
        (strings) and returns a string which is ready for evaluation using the
        metric functions in `metric_fns`. Can be set to None as a no-op.
      token_preprocessor: an optional function (or list of functions) that
        (each) takes in a tf.data.Dataset of token features and returns a
        tf.data.Dataset of token features.
        Can be set to None as a no-op. If a list is given, they will be
        executed sequentially.
        The functions are also passed `sequence_length` and `vocabulary`
        keyword arguments.
      tfds_data_dir: string, an optional path to a specific TFDS data directory
        to use.
      output_features: list(string), a list of the primary output features of
        the dataset that will be prepared for the model. Defaults to 'inputs'
        and 'targets'.
      splits: list(string) or None, a list of allowable splits to load. The
        default, None, uses all available splits from the TFDS dataset info.
    """
    if not _VALID_TASK_NAME_REGEX.match(name):
      raise ValueError(
          "Task name '%s' contains invalid characters. Must match regex: %s" % (
              name, _VALID_TASK_NAME_REGEX.pattern))
    if ":" not in tfds_name:
      raise ValueError(
          "TFDS name must contain a version number, got: %s" % tfds_name)
    self._name = name
    self._tfds = LazyTfdsLoader(tfds_name, tfds_data_dir)
    self._text_preprocessor = (
        [] if text_preprocessor is None else text_preprocessor)
    self._token_preprocessor = (
        [] if token_preprocessor is None else token_preprocessor)
    self._sentencepiece_model_path = sentencepiece_model_path
    self._metric_fns = metric_fns
    # Use a pass-through if postprocess_fn is not provided
    self._postprocess_fn = postprocess_fn or (lambda x, **unused_kwargs: x)
    self._cache_dir = None
    self._stats = {}
    self._output_features = sorted(
        set(output_features or _DEFAULT_FEATURE_KEYS))
    self._splits = splits

  @property
  def name(self):
    return self._name

  @property
  def tfds_dataset(self):
    return self._tfds

  @property
  def postprocess_fn(self):
    return self._postprocess_fn

  @property
  def metric_fns(self):
    return self._metric_fns

  @property
  def sentencepiece_model_path(self):
    return self._sentencepiece_model_path

  @property
  def output_features(self):
    return self._output_features

  @property
  def token_preprocessor(self):
    return self._token_preprocessor

  @property
  def splits(self):
    return self._splits or self.tfds_dataset.info.splits

  def _preprocess_dataset(self, dataset, preprocessors, **preprocess_kwargs):
    if not hasattr(preprocessors, "__iter__"):
      preprocessors = [preprocessors]
    for prep_fn in preprocessors:
      dataset = prep_fn(dataset, **preprocess_kwargs)
    return dataset

  def _validate_dataset(
      self,
      dataset,
      expected_output_type,
      expected_output_rank,
      error_label,
      ensure_no_eos=False):
    """Validates properties of a tf.data.Dataset, raising Exceptions if needed.

    Args:
      dataset: a tf.data.Dataset to validate.
      expected_output_type: a tf.dtype, the expected type of the model features.
      expected_output_rank: an int, the expected rank of the model features.
      error_label: a string, an identifier for the previous processing step to
        report in raised ValueErrors.
      ensure_no_eos: a bool, whether or not to verify that the model features
        contain no EOS tokens.

    Returns:
      a validated tf.data.Dataset.
    """
    types = tf.data.get_output_types(dataset)
    shapes = tf.data.get_output_shapes(dataset)
    for feat in self.output_features:
      if feat not in types:
        raise ValueError(
            "Task dataset is missing expected output feature after {label}: "
            "{feat}".format(label=error_label, feat=feat))
      if expected_output_type != types[feat]:
        raise ValueError(
            "Task dataset has incorrect type for feature '{feat}' after "
            "{label}: Got {actual}, expected {expected}".format(
                feat=feat, label=error_label, actual=types[feat].name,
                expected=expected_output_type.name))
      if expected_output_rank != len(shapes[feat]):
        raise ValueError(
            "Task dataset has incorrect rank for feature '{feat}' after "
            "{label}: Got {actual}, expected {expected}".format(
                feat=feat, label=error_label, actual=len(shapes[feat]),
                expected=expected_output_rank))

    def _ensure_no_eos(feat, v):
      if feat not in self.output_features:
        return v
      with tf.control_dependencies([
          tf.assert_none_equal(
              v, tf.constant(1, tf.int64),
              message="Feature '{feat}' unexpectedly contains EOS=1 token "
              "after {label}.".format(feat=feat, label=error_label))
      ]):
        return v
    if ensure_no_eos:
      dataset = dataset.map(
          lambda ex: {k: _ensure_no_eos(k, v) for k, v in ex.items()},
          num_parallel_calls=tf.data.experimental.AUTOTUNE)
    return dataset

  def preprocess_text(self, dataset):
    """Preprocessed text dataset."""
    dataset = self._preprocess_dataset(dataset, self._text_preprocessor)
    dataset = self._validate_dataset(
        dataset, expected_output_type=tf.string, expected_output_rank=0,
        error_label="text preprocessing")
    return dataset

  def preprocess_tokens(self, dataset, sequence_length):
    """Preprocesses tokenized dataset.

    Args:
      dataset: a tf.data.Dataset
      sequence_length: dict mapping feature key to int length for that feature
    Returns:
      a tf.data.Dataset
    """
    dataset = self._preprocess_dataset(
        dataset, self._token_preprocessor,
        sequence_length=sequence_length,
        vocabulary=self.get_vocabulary())
    dataset = self._validate_dataset(
        dataset,
        expected_output_type=tf.int64,
        expected_output_rank=1,
        error_label="token preprocessing",
        ensure_no_eos=True)
    # Trim and append EOS=1 token to model features.
    def _trim_and_append_eos(feat, v):
      if feat not in self.output_features:
        return v
      return tf.concat([v[:sequence_length[feat]-1], [1]], axis=0)

    return dataset.map(
        lambda ex: {k: _trim_and_append_eos(k, v) for k, v in ex.items()},
        num_parallel_calls=tf.data.experimental.AUTOTUNE)

  def initialize(self):
    """Attempts to load cached dataset and stats."""
    if self._cache_dir:
      return

    # See if cached data exists in any of the cache directories.
    potential_cache_dirs = [
        os.path.join(d, self.name) for d in _GLOBAL_CACHE_DIRECTORIES]
    for cache_dir in potential_cache_dirs:
      if tf.io.gfile.exists(os.path.join(cache_dir, "COMPLETED")):
        self._cache_dir = cache_dir
        logging.info("'%s' is cached at %s.", self.name, self.cache_dir)
        return
    logging.info(
        "'%s' does not exist in any task cache directories (searched %s).",
        self.name,
        potential_cache_dirs,
    )

  @property
  def cached(self):
    """Returns whether or not cached dataset exists, initializing if needed."""
    self.initialize()
    return self._cache_dir is not None

  @property
  def cache_dir(self):
    """Returns the cache directory, initializing if needed."""
    self.assert_cached()
    return self._cache_dir

  def assert_cached(self):
    """Raises an assertion error if cached dataset does not exist."""
    assert self.cached, (
        "'%s' does not exist in any of the task cache directories" % self.name)

  def get_cached_stats(self, split=tfds.Split.TRAIN):
    """Returns basic statistics for cached dataset."""
    self.assert_cached()
    if split not in self._stats:
      stats_path = get_stats_path(self.cache_dir, split)
      if not tf.io.gfile.exists(stats_path):
        raise ValueError(
            "Stats do not exist for '%s' split: %s" % (self.name, split))
      with tf.io.gfile.GFile(stats_path) as f:
        self._stats[split] = json.load(f)
    return self._stats[split]

  def get_vocabulary(self):
    """Returns a SentencePieceVocabulary object using the Task's model."""
    return sentencepiece_vocabulary.SentencePieceVocabulary(
        self.sentencepiece_model_path)

  def get_dataset(
      self,
      sequence_length,
      split=tfds.Split.TRAIN,
      use_cached=True,
      shuffle=True,
      shuffle_buffer_size=_SHUFFLE_BUFFER_SIZE,
  ):
    """Returns a tf.data.Dataset from cache or generated on the fly.

    Args:
      sequence_length: dict mapping feature key to int length for that feature
      split: string, the split to return.
      use_cached: bool, whether to use the cached dataset instead of processing
        it on the fly. Defaults to True.
      shuffle: bool, whether to shuffle the dataset.  Only used when generating
        on the fly (use_cached=False).
      shuffle_buffer_size: an integer
    Returns:
      A mixed tf.data.Dataset.
    """
    if use_cached:
      ds = self._get_cached_dataset(split, shuffle)
    else:
      ds = self.tfds_dataset.load(split, shuffle_files=shuffle)
      ds = self.preprocess_text(ds)
      # Tokenize
      ds = encode_string_features(
          ds, self.get_vocabulary(), keys=self.output_features,
          copy_plaintext=True)

    # Post tokenization processing.
    ds = self.preprocess_tokens(ds, sequence_length)

    if shuffle:
      # Shuffle before mixing since preprocessor can output multiple
      # (correlated) examples per input.
      ds = ds.shuffle(shuffle_buffer_size)

    return ds

  def _get_cached_dataset(self, split=tfds.Split.TRAIN, shuffle=True):
    """Returns a tf.data.Dataset read from cached files."""
    self.assert_cached()
    with tf.io.gfile.GFile(get_info_path(self.cache_dir, split)) as f:
      split_info = json.load(f)

    # Use `FixedLenSequenceFeature` for sequences with variable length.
    def _feature_config(shape, dtype):
      if shape and shape[0] is None:
        return tf.io.FixedLenSequenceFeature(
            shape[1:], dtype, allow_missing=True)
      return tf.io.FixedLenFeature(shape, dtype)
    feature_desc = {
        feat: _feature_config(**desc)
        for feat, desc in split_info["features"].items()}

    ds = tf.data.Dataset.list_files(
        "%s-*-of-*%d" % (
            get_tfrecord_prefix(self.cache_dir, split),
            split_info["num_shards"]),
        shuffle=shuffle)
    ds = ds.interleave(
        tf.data.TFRecordDataset,
        cycle_length=16, block_length=16,
        num_parallel_calls=tf.data.experimental.AUTOTUNE)
    ds = ds.map(lambda ex: tf.parse_single_example(ex, feature_desc),
                num_parallel_calls=tf.data.experimental.AUTOTUNE)
    if self.get_cached_stats(split)["examples"] <= _MAX_EXAMPLES_TO_MEM_CACHE:
      ds = ds.cache()
    return ds


class TaskRegistry(DatasetProviderRegistry):
  _REGISTRY = {}
  _PROVIDER_TYPE = Task

  @classmethod
  def add(cls, name, **kwargs):
    super(TaskRegistry, cls).add(name, Task, name, **kwargs)


# ================================ Mixtures ====================================
class Mixture(DatasetProviderBase):
  """Class for mixing multiple tasks."""

  def __init__(self, tasks, default_rate=None):
    """Mixture constructor.

    A mixture specifies a set of tasks with associated mixing rates.

    Mixing happens on preprocessed tokenized examples.

    The mixing rates represent relative numbers of examples to use from their
    associated tasks.  Setting the mixing rates to be equal to the numbers of
    examples in the tasks will result in each task going through an epoch in
    about the same amount of time - i.e. all examples are sampled equally across
    all tasks.

    Rates can be expressed either as absolute numbers or as functions that
    receive the Task as an argument.

    Args:
      tasks: a list where each element is either a string (task name) or a
        pair whose first element is the task name and whose second element
        is either a float (rate) or a function from Task to float.
      default_rate: a float or a function from Task to float. This specifies the
        default rate if rates are not provided in the `tasks` argument.
    """
    self._task_to_rate = {}
    self._tasks = []
    for t in tasks:
      if isinstance(t, str):
        task_name = t
        rate = default_rate
        if default_rate is None:
          raise ValueError("need a rate for each task")
      else:
        task_name, rate = t
      self._tasks.append(TaskRegistry.get(task_name))
      self._task_to_rate[task_name] = rate
    if len(set(tuple(t.output_features) for t in self._tasks)) != 1:
      raise ValueError(
          "All Tasks in a Mixture must have the same output features."
      )
    if len(set(t.sentencepiece_model_path for t in self._tasks)) != 1:
      raise ValueError(
          "All Tasks in a Mixture must have the same sentencepiece_model_path."
      )

  @property
  def tasks(self):
    return self._tasks

  def get_rate(self, task):
    rate = self._task_to_rate[task.name]
    return float(rate(task) if callable(rate) else rate)

  @property
  def output_features(self):
    # We require all tasks to have the same output_features in __init__
    # so we can just get the output_features for the 0th task
    return self._tasks[0].output_features

  @property
  def sentencepiece_model_path(self):
    # We require all tasks to have the same sentencepiece_model_path in __init__
    # so we can just get the sentencepiece_model_path for the first task
    return self._tasks[0].sentencepiece_model_path

  def get_vocabulary(self):
    """Returns a SentencePieceVocabulary object using the Tasks' model."""
    return self._tasks[0].get_vocabulary()

  def get_dataset(
      self,
      sequence_length,
      split=tfds.Split.TRAIN,
      use_cached=True,
      shuffle=True,
      compute_stats_empirically=False,
  ):
    """Returns the dataset of mixed tasks using the object-specified rates.

    Args:
      sequence_length: dict mapping feature key to int length for that feature
      split: string, the split to return for all tasks.
      use_cached: bool, whether to use the cached dataset instead of processing
        it on the fly. Defaults to True.
      shuffle: bool, whether to shuffle the dataset.  Only used when generating
        on the fly (use_cached=False).
      compute_stats_empirically: a boolean - does not work on TPU
    """
    tasks = []
    for task in self.tasks:
      if split not in task.splits:
        logging.info(
            "Task %s has no '%s' split, skipping.", task.name, split
        )
        continue
      tasks.append(task)
    if not tasks:
      raise ValueError("No datasets have a '{}' split".format(split))
    def filter_features(ex):
      return {k: v for k, v in ex.items() if k in self.output_features}
    datasets = [
        task.get_dataset(sequence_length, split, use_cached, shuffle=shuffle)  # pylint:disable=g-complex-comprehension
        .repeat()
        .map(filter_features, num_parallel_calls=tf.data.experimental.AUTOTUNE)
        for task in tasks]
    rates = [self.get_rate(task) for task in tasks]
    # Sample from the dataset with the rates rates
    dataset = tf.data.experimental.sample_from_datasets(datasets, rates)
    if split == "train" and use_cached:
      _log_mixing_proportions(tasks, datasets, rates, dataset, sequence_length,
                              compute_stats_empirically)
    return dataset

# Functions to be used as mixing rates:


@gin.configurable
def rate_num_examples(task, maximum=None, temperature=1.0, scale=1.0):
  """Mixing rate equal to the number of examples for the task."""
  ret = task.get_cached_stats("train")["examples"]
  ret *= scale
  if maximum:
    ret = min(ret, maximum)
  if temperature != 1.0:
    ret = ret ** (1.0 / temperature)
  return ret


@gin.configurable
def rate_unsupervised(task, value=1e6):
  """Gin-configurable mixing rate for the unsupervised co-training task."""
  del task
  return value


def _log_padding_fractions(dataset, sequence_length, num_examples=100):
  """Empirically compute the fraction of padding - log the results.

  Args:
    dataset: a tf.data.Dataset
    sequence_length: dict from string to int (packed lengths)
    num_examples: an integer
  """
  logging.info("computing padding fractions")
  keys = sequence_length.keys()
  padding_frac = {k: 0 for k in keys}
  for ex in tfds.as_numpy(dataset.take(num_examples)):
    for k in keys:
      padding_frac[k] += 1 - (sequence_length[k] / len(ex[k]))
  for k in keys:
    logging.info("%s padding fraction = %g", k, padding_frac[k])


def _log_mixing_proportions(
    tasks, datasets, rates, mixed_dataset,
    sequence_length, compute_stats_empirically):
  """Log information about the mixing proportions.

  Called from Mixture.get_dataset.

  Args:
    tasks: a list of Task
    datasets: a list of tf.data.Dataset
    rates: a list of floats
    mixed_dataset: a tf.data.Dataset
    sequence_length: dict from string to int (packed lengths)
    compute_stats_empirically: a boolean - does not work on TPU
  """
  def _normalize(l):
    denom = sum(l)
    return [x / denom for x in l]
  # compute some stats about the mixture
  examples_fraction = _normalize(rates)
  if compute_stats_empirically:
    stats_examples = 100
    mean_inputs_length = []
    mean_targets_length = []
    for dataset in datasets:
      inputs_sum = 0
      targets_sum = 0
      for ex in tfds.as_numpy(dataset.take(stats_examples)):
        inputs_sum += ex["inputs"].size
        targets_sum += ex["targets"].size
      mean_inputs_length.append(inputs_sum / float(stats_examples))
      mean_targets_length.append(targets_sum / float(stats_examples))
  else:
    def _estimated_mean_length(task, key):
      if task.token_preprocessor:
        return sequence_length[key]
      else:
        return min(sequence_length[key],
                   (task.get_cached_stats("train")[key + "_tokens"] /
                    task.get_cached_stats("train")["examples"]))
    mean_inputs_length = [_estimated_mean_length(task, "inputs")
                          for task in tasks]
    mean_targets_length = [_estimated_mean_length(task, "targets")
                           for task in tasks]
  inputs_fraction = _normalize(
      [l * r for l, r in zip(mean_inputs_length, rates)])
  targets_fraction = _normalize(
      [l * r for l, r in zip(mean_targets_length, rates)])
  logging.info("%12s %12s %12s %12s %12s %12s %s",
               "rate", "ex.frac.", "inp.frac.", "tgt.frac.",
               "inp.len.", "tgt.len", "task")
  for i in range(len(rates)):
    logging.info("%12g %12g %12g %12g %12g %12g %s",
                 rates[i], examples_fraction[i],
                 inputs_fraction[i], targets_fraction[i],
                 mean_inputs_length[i], mean_targets_length[i],
                 tasks[i].name)
  if compute_stats_empirically:
    _log_padding_fractions(mixed_dataset, sequence_length)


class MixtureRegistry(DatasetProviderRegistry):
  _REGISTRY = {}
  _PROVIDER_TYPE = Mixture

  @classmethod
  def add(cls, name, tasks, default_rate=None):
    super(MixtureRegistry, cls).add(name, Mixture, tasks, default_rate)
