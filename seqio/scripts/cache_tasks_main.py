# Copyright 2021 The SeqIO Authors.
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

# Lint as: python3
r"""Dumps preprocessed tasks as TFRecord of tf.Examples.

Usage:
====================
t5_cache_tasks \
--tasks=my_task_*,your_task \
--excluded_tasks=my_task_5 \
--output_cache_dir=/path/to/cache_dir \
--module_import=my.tasks \
--alsologtostderr

"""

import importlib
import json
import os
import re

from absl import app
from absl import flags
from absl import logging

import apache_beam as beam
import apache_beam.metrics as metrics
import numpy as np
import seqio
import tensorflow.compat.v2 as tf



# Significantly speeds up preprocessing in tf1.
tf.compat.v1.enable_eager_execution()

FLAGS = flags.FLAGS

flags.DEFINE_list(
    "tasks", None,
    "Regexes matching task(s) to build a preprocessed dataset for. Will build "
    "all registered if not specified.")
flags.DEFINE_list(
    "excluded_tasks", None,
    "Regexes matching task(s) to skip.")
flags.DEFINE_string(
    "output_cache_dir", None,
    "The directory to output cached tasks to.")
flags.DEFINE_integer(
    "max_input_examples", None,
    "The maximum number of input examples to use. No limit if None.")
flags.DEFINE_list(
    "tasks_additional_cache_dirs", [],
    "Additional directories to search for cached Tasks after checking the "
    "global caches and `output_cache_dir`.")
flags.DEFINE_multi_string(
    "module_import", [],
    "Modules to import. Use this, for example, to add new `Task`s to the "
    "global `TaskRegistry`.")
flags.DEFINE_list(
    "pipeline_options", ["--runner=DirectRunner"],
    "A comma-separated list of command line arguments to be used as options "
    "for the Beam Pipeline.")
flags.DEFINE_boolean(
    "overwrite", False,
    "If true, overwrite the cached task even if it exists in the cached "
    "directories.")


def _import_modules(modules):
  for module in modules:
    if module:
      importlib.import_module(module)


class PreprocessTask(beam.PTransform):
  """Abstract base class to preprocess a Task.

  Returns a PCollection of example dicts containing Tensors.
  """

  def __init__(
      self, task, split, max_input_examples=None, modules_to_import=()):
    """BasePreprocessTask constructor.

    Args:
      task: Task, the task to process.
      split: string, the split to process.
      max_input_examples: (Optional) int, the maximum number of input examples
        to use.
      modules_to_import: (Optional) list, modules to import.
    """
    self._task = task
    self._max_input_examples = max_input_examples
    self._split = split
    self._modules_to_import = modules_to_import
    self.shards = list(range(len(task.source.list_shards(split))))
    logging.info(
        "%s %s shards: %s", task.name, split, ", ".join(
            ["%s" % f for f in self.shards]))

  def _increment_counter(self, name):
    metrics.Metrics.counter(
        str("%s_%s" % (self._task.name, self._split)), name).inc()

  def _emit_examples(self, shard_index):
    """Emits examples keyed by shard number and index for a single shard."""
    _import_modules(self._modules_to_import)
    logging.info("Processing shard: %d", shard_index)
    self._increment_counter("input-shards")

    ds = self._task.source.get_dataset(
        split=self._split,
        shard_info=seqio.ShardInfo(
            index=shard_index, num_shards=len(self.shards)
        ),
        shuffle=False)

    if self._max_input_examples:
      num_shard_examples = int(self._max_input_examples / len(self.shards))
      ds = ds.repeat().take(num_shard_examples)

    ds = ds.prefetch(tf.data.AUTOTUNE)

    ds = self._task.preprocess_precache(ds)

    for i, ex in enumerate(ds.as_numpy_iterator()):
      self._increment_counter("examples")
      # Log every power of two.
      if i & (i - 1) == 0:
        logging.info("Example [%d] = %s", i, ex)
      yield ex

  def expand(self, pipeline):
    # The Reshuffles allow for better parallelism.
    return (pipeline
            | "create_shards" >> beam.Create(self.shards)
            | "shard_reshuffle" >> beam.Reshuffle()
            | "emit_examples" >> beam.FlatMap(self._emit_examples)
            | "example_reshuffle" >> beam.Reshuffle())


class WriteExampleTfRecord(beam.PTransform):
  """Writes examples (dicts) to a TFRecord of tf.Example protos."""

  def __init__(self, output_path, num_shards=None):
    """WriteExampleTfRecord constructor.

    Args:
      output_path: string, path to the output TFRecord file (w/o shard suffix).
      num_shards: (optional) int, number of shards to output or None to use
        liquid sharding.
    """
    self._output_path = output_path
    self._num_shards = num_shards

  def expand(self, pcoll):
    return (
        pcoll
        | beam.Map(seqio.dict_to_tfexample)
        | beam.Reshuffle()
        | beam.io.tfrecordio.WriteToTFRecord(
            self._output_path,
            num_shards=self._num_shards,
            coder=beam.coders.ProtoCoder(tf.train.Example)))


class WriteJson(beam.PTransform):
  """Writes datastructures to file as JSON(L)."""

  def __init__(self, output_path, prettify=True):
    """WriteJson constructor.

    Args:
      output_path: string, path to the output JSON(L) file.
      prettify: bool, whether to write the outputs with sorted keys and
        indentation. Note this not be used if there are multiple records being
        written to the file (JSONL).
    """
    self._output_path = output_path
    self._prettify = prettify

  def _jsonify(self, el):
    if self._prettify:
      return json.dumps(el, sort_keys=True, indent=2)
    else:
      return json.dumps(el)

  def expand(self, pcoll):
    return (
        pcoll
        | beam.Map(self._jsonify)
        | "write_info" >> beam.io.WriteToText(
            self._output_path,
            num_shards=1,
            shard_name_template=""))


class GetInfo(beam.PTransform):
  """Computes info for dataset examples.

  Expects a single PCollections of examples.
  Returns a dictionary with information needed to read the data (number of
  shards, feature shapes and types)
  """

  def __init__(self, num_shards):
    self._num_shards = num_shards

  def _info_dict(self, ex):
    if not ex:
      return {}
    assert len(ex) == 1
    ex = ex[0]
    info = {
        "num_shards": self._num_shards,
        "features": {},
        "seqio_version": seqio.__version__,
    }
    feature_dict = info["features"]
    for k, v in ex.items():
      t = tf.constant(v)
      dtype = t.dtype.name
      shape = [None] * len(t.shape)
      feature_dict[k] = {"shape": shape, "dtype": dtype}
    return info

  def expand(self, pcoll):
    return (
        pcoll
        | beam.combiners.Sample.FixedSizeGlobally(1)
        | beam.Map(self._info_dict))


class GetStats(beam.PTransform):
  """Computes stastistics for dataset examples.

  Expects a dictionary of string identifiers mapped to PCollections of examples.
  Returns a dictionary with statistics (number of examples, number of tokens)
  prefixed by the identifiers.
  """

  def __init__(self, output_features):
    self._output_features = output_features

  def expand(self, pcoll):
    to_dict = lambda x: {x[0]: x[1]}
    example_counts = (
        pcoll
        | "count_examples" >> beam.combiners.Count.Globally()
        | "key_example_counts" >> beam.Map(
            lambda x: ("examples", x))
        | "example_count_dict" >> beam.Map(to_dict))
    def _count_tokens(pcoll, feat):

      def _count(ex):
        if (feat in ex and isinstance(ex[feat], np.ndarray) and
            ex[feat].dtype in (np.int32, np.int64)):
          yield ("%s_tokens" % feat, int(sum(ex[feat] > 1)))

      return pcoll | "key_%s_toks" % feat >> beam.FlatMap(_count)

    token_counts = (
        [_count_tokens(pcoll, feat)
         for feat in self._output_features]
        | "flatten_tokens" >> beam.Flatten())
    total_tokens = (
        token_counts
        | "sum_tokens" >> beam.CombinePerKey(sum)
        | "token_count_dict" >> beam.Map(to_dict))
    max_tokens = (
        token_counts
        | "max_tokens" >> beam.CombinePerKey(max)
        | "rename_max_stat" >> beam.Map(
            lambda x: (x[0].replace("tokens", "max_tokens"), x[1]))
        | "token_max_dict" >> beam.Map(to_dict))

    def _merge_dicts(dicts):
      merged_dict = {}
      for d in dicts:
        assert not set(merged_dict).intersection(d)
        merged_dict.update(d)
      return merged_dict
    return (
        [example_counts, total_tokens, max_tokens]
        | "flatten_counts" >> beam.Flatten()
        | "merge_stats" >> beam.CombineGlobally(_merge_dicts))


def run_pipeline(
    pipeline, task_names, cache_dir, max_input_examples=None,
    excluded_tasks=None, modules_to_import=(), overwrite=False,
    completed_file_contents=""):
  """Run preprocess pipeline."""
  output_dirs = []
  # Includes all names by default.
  included_regex = re.compile(r"(%s\Z)" % r"\Z|".join(task_names or [".*"]))
  # Excludes only empty names by default.
  excluded_regex = re.compile(r"(%s\Z)" % r"\Z|".join(excluded_tasks or []))
  task_names = [
      t for t in seqio.TaskRegistry.names()
      if included_regex.match(t) and not excluded_regex.match(t)]
  for task_name in task_names:
    task = seqio.TaskRegistry.get(task_name)
    if not task.supports_caching:
      logging.info(
          "Skipping task that does not support caching: '%s'", task.name)
      continue

    task_cache_dir = task.cache_dir
    output_dir = os.path.join(
        cache_dir, seqio.get_task_dir_from_name(task.name))

    if task_cache_dir and not overwrite:
      logging.info("Skipping task '%s', which exists in cache dir: %s",
                   task.name, task_cache_dir)
      continue

    if task_cache_dir and overwrite:
      if task_cache_dir == output_dir:
        # We were asked to overwrite the data, and the given directory that we
        # should generate the data in already has the data, then delete it.
        logging.warning(
            "Overwriting already cached data for task '%s' in cache_dir %s",
            task.name, output_dir)
        tf.io.gfile.rmtree(output_dir)
      else:
        # Cannot overwrite, since cache_dir isn't same as task.cache_dir.
        logging.warning("Not overwriting data in task.cache_dir since it is "
                        "different from cache_dir - %s vs %s", task.cache_dir,
                        output_dir)
        continue

    if not task.splits:
      logging.warning("Skipping task '%s' with no splits.", task.name)
      continue

    # Log this task to the terminal.
    print("Caching task '%s' with splits: %s" % (task.name, task.splits))

    output_dirs.append(output_dir)
    completion_values = []

    if isinstance(task.source, seqio.FunctionDataSource):
      logging.warning(
          "Task '%s' using FunctionDataSource cannot be distributed. If your "
          "dataset is large, you may be able to speed up preprocessing by "
          "sharding it and using a TfdsSource, TFExampleSource, or "
          "TextLineSource instead.", task.name)

    for split in task.splits:
      label = "%s_%s" % (task.name, split)

      pat = PreprocessTask(task, split, max_input_examples, modules_to_import)
      num_shards = len(pat.shards)
      examples = pipeline | "%s_pat" % label >> pat
      completion_values.append(
          examples
          | "%s_write_tfrecord" % label >> WriteExampleTfRecord(
              seqio.get_cached_tfrecord_prefix(output_dir, split),
              num_shards=num_shards))
      completion_values.append(
          examples
          | "%s_info" % label >> GetInfo(num_shards)
          | "%s_write_info" % label >> WriteJson(
              seqio.get_cached_info_path(output_dir, split)))
      completion_values.append(
          examples
          | "%s_stats" % label >> GetStats(task.output_features)
          | "%s_write_stats" % label >> WriteJson(
              seqio.get_cached_stats_path(output_dir, split)))

    # After all splits for this task have completed, write COMPLETED files to
    # the task's output directory.
    _ = (completion_values
         | "%s_flatten_completion_values" % task.name >> beam.Flatten()
         | "%s_discard_completion_values" % task.name >> beam.Filter(
             lambda _: False)
         | "%s_write_completed_file" % task.name >> beam.io.textio.WriteToText(
             os.path.join(output_dir, "COMPLETED"),
             append_trailing_newlines=False, num_shards=1,
             shard_name_template="", header=completed_file_contents))

  return output_dirs


def main(_):
  flags.mark_flags_as_required(["output_cache_dir"])

  _import_modules(FLAGS.module_import)

  seqio.add_global_cache_dirs(
      [FLAGS.output_cache_dir] + FLAGS.tasks_additional_cache_dirs)

  pipeline_options = beam.options.pipeline_options.PipelineOptions(
      FLAGS.pipeline_options)
  with beam.Pipeline(options=pipeline_options) as pipeline:
    tf.io.gfile.makedirs(FLAGS.output_cache_dir)
    unused_output_dirs = run_pipeline(
        pipeline, FLAGS.tasks, FLAGS.output_cache_dir,
        FLAGS.max_input_examples, FLAGS.excluded_tasks, FLAGS.module_import,
        FLAGS.overwrite,
    )


def console_entry_point():
  app.run(main)

if __name__ == "__main__":
  console_entry_point()
