# T5: Text-To-Text Transfer Transformer

T5 serves primarily as code for reproducing the experiments in [_Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer_][paper].
The bulk of the code in this repository is used for loading, preprocessing, mixing, and evaluating datasets.
It also provides a way to fine-tune the [pre-trained models](#released-model-checkpoints) released alongside the publication.

T5 can be used as a library for future model development by providing useful modules for training and fine-tuning (potentially *huge*) models on mixtures of text-to-text tasks.

## Organization

T5 is organized into 3 core packages plus configurations for reproducing experiments from the [paper][paper]:

#### t5.data

`t5.data` is a library for defining `Task` objects that provide `tf.data.Dataset`s. Each `Task` references a dataset from [TensorFlow Datasets][tfds] along with a preprocesssing function for converting the dataset into the appropriate format for a text-to-text model with fields for `inputs` and `targets`.  For example, the `translate` preprocessor converts inputs in the form

```py
{'de': 'Das ist gut.', 'en': 'That is good.'}
```

to the form

```py
{'inputs': 'translate German to English: Das ist gut.', 'targets': 'That is good.'}
```

`Task` objects also handle tokenization of strings, optional preprocessing of the token representation (e.g., corruptions for unsupservised training), and specification of associated metrics for evaluation.

Finally, `t5.data` contains a `Mixture` class that can be instantiated to combine multiple `Task` datasets for multi-task training using various functions for specifying the mixture rates.

#### t5.evaluation

`t5.evaluation` contains two core components: a module for specifying metrics to be used during evaluation and utilities for applying these metrics at evaluation time.

#### t5.models

`t5.models` contains shims for connecting T5 `Tasks` and `Mixtures` to a model implementation for training, evaluation, and inference. Currently the only available shim is to the [Mesh TensorFlow Transformer][mtft], which enables both data and model parallelism for training massive Transformer models. It also includes a binary for launching the model along with [gin config][gin] files for setting various hyperparameters.

## Usage

Here we provide example usage for how to pre-train, fine-tune, evaluate, and decode from a model with our codebase. You can use these instructions to reproduce our results, fine-tune one of our released checkpoints with your own data and/or hyperparameters, or pre-train a model from scratch.

### Datasets

We use [TensorFlow Datasets (TFDS)][tfds] as our dataset repository. When you select a dataset and run our training binary (see instructions below), the dataset will automatically be downloaded and prepared on its first use. After preparation is complete, the dataset is cached to your local storage to avoid this overhead in future runs.  If working in the cloud, we recommend you set the `--t5_tfds_data_dir` flag to point to a persistent storage location, such as a [GCS bucket][gcs]. This is a requirement when training on TPU.

Note that the [C4][c4] dataset we created for unsupervised pre-training requires a significant amount of bandwith for downloading the raw [Common Crawl][cc] scrapes and compute for its preparation. We suggest you take advantage of the [Apache Beam][beam] support in TFDS, which enables distributed preprocessing of the dataset and can be run on [Google Cloud Dataflow][gcd]. Otherwise, it is unlikely that you will be able to complete preprocessing in a human lifetime. Read more in the [TFDS Beam instructions][tfds_beam].

### Installation

To install the T5 package, simply run:

```sh
pip install t5[gcp]
```

### Setting up TPUs on GCP for training and evaluation

You will first need to launch a Virtual Machine (VM) on Google Cloud. Details about launching the VM can be found at the [Google Cloud Documentation](http://cloud/compute/docs/instances/create-start-instance).

In order to run training or eval on Cloud TPUs, you must set up the following variables based on your project, zone and GCS bucket appropriately. Please refer to the [Cloud TPU Quickstart](https://cloud.google.com/tpu/docs/quickstart) guide for more details.

```sh
export PROJECT=your_project_name
export ZONE=your_project_zone
export BUCKET=gs://yourbucket/
export TPU_NAME=t5-tpu
export DATA_DIR="${BUCKET}/your_data_dir"
export MODEL_DIR="${BUCKET}/your_model_dir"
```

Please use the following command to create a TPU device in the Cloud VM.

```sh
ctpu up --name=$TPU_NAME --project=$PROJECT --zone=$ZONE --tpu-size=v3-8  \
        --tpu-only   --tf-version=1.15.dev20190821 --noconf
```


### Training

In the command below, we train a model on the [GLUE Benchmark](https://gluebenchmark.com/) MRPC task from scratch. You can change the `MIXTURE_NAME` gin parameter to use any of the tasks or mixtures provided in our package.

```sh
t5_mesh_transformer  \
  --tpu="${TPU_NAME}" \
  --gcp_project="${PROJECT}" \
  --tpu_zone="${ZONE}" \
  --model_dir="${MODEL_DIR}" \
  --t5_tfds_data_dir=${DATA_DIR} \
  --gin_file="dataset.gin" \
  --gin_file="models/bi_v1.gin" \
  --gin_param="utils.tpu_mesh_shape.model_parallelism = 1" \
  --gin_param="utils.tpu_mesh_shape.tpu_topology = '2x2'" \
  --gin_param="MIXTURE_NAME = 'glue_mrpc_v002'"
```

The full list of tasks and mixtures can be obtained by running:

```sh
python -c "import t5; print(t5.data.MixtureRegistry.names())"
```

### Fine-tuning

In order to fine-tune one of our [pre-trained models](#released-model-checkpoints), you need to pass the operative config and pre-trained checkpoint to the training script. The operative config should be passed in as a `gin_file` flag. It specifies the model architecture and other hyperparameters. For example, to use the T5-small model:

```sh
--gin_file="gs://t5-data/pretrained_models/small/operative_config.gin"
```

The correct pre-trained checkpoint path is included in the operative config.
Then, you need to specify a mixture to fine-tune on. Say you want to fine-tune on the MRPC task from GLUE, which is called glue_mrpc_v002. You can specify this mixture for fine-tuning by setting:

```sh
--gin_param="MIXTURE_NAME = 'glue_mrpc_v002'"
```

Alternatively, you could fine-tune with a TSV file where each line is formatted as `<input>\t<target>`. For example, you could try one of the paired translation datasets from WMT '19 [News Commentary 14](http://data.statmt.org/news-commentary/v14/training/) training set
(e.g., [English-French](http://data.statmt.org/news-commentary/v14/training/)). When using a TSV file, you would replace the `MIXTURE_NAME` flag with:

```sh
--gin_param="utils.run.train_dataset_fn = @t5.models.mesh_transformer.tsv_dataset_fn"
--gin_param="tsv_dataset_fn.filename = 'gs:/path/to/tsv'"
```

To fine-tune with the same hyperparameters we used in the [paper][paper] (using a constant learning rate of 0.001), you can pass in this gin file which is included in the T5 package:

```
--gin_file="learning_rate_schedules/constant_0_001.gin"
```

The operative config for the pre-trained models are set so that there is effectively no limit on the number of train steps. If you'd like to train for a specific number of steps, you'll need to pass that in. Since the pre-trained model has already been trained for 1,000,000 steps, you should specify the total number of steps after pre-training and fine-tuning. For example, if you want to fine-tune for an additional 10,000 steps, you should pass

```
--gin_param="run.train_steps = 1010000"
```

You can also use a different batch size for fine-tuning. We set the batch size according to the total number of tokens in a batch. By default, a batch uses a sequence length of 512. To set the number of tokens in a batch, you should set

```
--gin_param = "tokens_per_batch=1048576"
```

### Eval

In order to evaluate a model in the T5 framework, you need to use the `eval.gin` file, specify the model directory, decoding method, and which checkpoint step(s) to evaluate. So, to evaluate on the GLUE MRPC task using beam search on *all* checkpoints, use the following command:

```sh
t5_mesh_transformer \
  --tpu="${TPU_NAME}" \
  --gcp_project="${PROJECT}" \
  --tpu_zone="${ZONE}" \
  --model_dir="${MODEL_DIR}" \
  --gin_file="${MODEL_DIR}/operative_config.gin" \
  --t5_tfds_data_dir=${DATA_DIR} \
  --gin_file="eval.gin" \
  --gin_file="beam_search.gin" \
  --gin_param="utils.tpu_mesh_shape.tpu_topology = '2x2'" \
  --gin_param="MIXTURE_NAME = 'glue_mrpc_v002'" \
  --gin_param="eval_checkpoint_step = 'all'"
```

To evaluate a specific checkpoint, simply set the `eval_checkpoint_step` parameter to appropriate checkpoint.

```
--gin_param="eval_checkpoint_step = 100000"
```

You can also use `greedy_decode.gin` or `sample_decode.gin` instead of `beam_search.gin` in the command above.


#### Decode

In order to produce predictions from a model in the T5 framework, you need to specify the model directory, decoding method, and which checkpoint step(s) to use for decoding. Assuming you have a text file of input sequences stored at `/path/to/intputs.txt`, an example command would be:

```sh
t5_mesh_transformer \
  --tpu="${TPU_NAME}" \
  --gcp_project="${PROJECT}" \
  --tpu_zone="${ZONE}" \
  --model_dir="${MODEL_DIR}" \
  --gin_file="${MODEL_DIR}/operative_config.gin" \
  --gin_file="infer.gin" \
  --gin_file="sample_decode.gin" \
  --gin_param="input_filename = '/path/to/inputs.txt'"\
  --gin_param="output_filename = '/tmp/outputs.txt'"\
  --gin_param="utils.tpu_mesh_shape.tpu_topology = '2x2'"\
  --gin_param="infer_checkpoint_step = 'all'"
```

To predict with a specific checkpoint, simply set the `infer_checkpoint_step` parameter to appropriate checkpoint.

```
--gin_param="infer_checkpoint_step = 100000"
```

You can also use `beam_search.gin` or `greedy_decode.gin` instead of `sample_decode.gin` in the command above.

### Reproducing our experiments

We provide operative configs for all of the experiments in the [paper][paper] in [gs://t5-data/experiments](https://console.cloud.google.com/storage/browser/t5-data/experiments).
The `experiments` folder has different subdirectories corresponding to the different sections in our paper.
For example, [gs://t5-data/experiments/objectives](https://console.cloud.google.com/storage/browser/t5-data/experiments/objectives) contains the experiments from Section 3.3 ("Unsupervised objectives").
Each subdirectory of the `objectives` folder contains operative configs for some particular experiment (where loosely speaking an "experiment" is one of the rows in one of the tables in our paper).

Let's say you want to reproduce the results for the "Prefix language modeling" objective (the first row in Table 4).
The operative configs for that experiment live in [gs://t5-data/experiments/objectives/obj-prefix_lm](https://console.cloud.google.com/storage/browser/t5-data/experiments/objectives/obj-prefix_lm).
In the base directory, there is an operative config for pre-training the model ([gs://t5-data/experiments/objectives/obj-prefix_lm/operative_config.gin](https://console.cloud.google.com/storage/browser/t5-data/experiments/objectives/obj-prefix_lm/operative_config.gin)).
Then, there are subdirectories for each of the downstream fine-tuning mixtures we consider, each of which has its own operative config (for example, [gs://t5-data/experiments/objectives/obj-prefix_lm/cnn_dailymail_v002/operative_config.gin](https://console.cloud.google.com/storage/browser/t5-data/experiments/objectives/obj-prefix_lm/cnn_dailymail_v002/operative_config.gin)).
To run this experiment, first pre-train a model with the pre-training operative config:

```sh
export PRETRAIN_MODEL_DIR="${BUCKET}/obj-prefix_lm"
t5_mesh_transformer  \
  --tpu="${TPU_NAME}" \
  --gcp_project="${PROJECT}" \
  --tpu_zone="${ZONE}" \
  --model_dir="${PRETRAIN_MODEL_DIR}" \
  --gin_file="gs://t5-data/experiments/objectives/obj-prefix_lm/operative_config.gin" \
  --gin_param="utils.tpu_mesh_shape.model_parallelism = 1" \
  --gin_param="utils.tpu_mesh_shape.tpu_topology = '2x2'"
```

Then, you can fine-tune the pre-trained model on CNN/Daily Mail like so:

```sh
export FINETUNE_MODEL_DIR="${BUCKET}/obj-prefix_lm/cnn_dailymail_v002"
t5_mesh_transformer  \
  --tpu="${TPU_NAME}" \
  --gcp_project="${PROJECT}" \
  --tpu_zone="${ZONE}" \
  --model_dir="${FINETUNE_MODEL_DIR}" \
  --gin_file="gs://t5-data/experiments/objectives/obj-prefix_lm/cnn_dailymail_v002/operative_config.gin" \
  --gin_param="init_checkpoint = '${PRETRAIN_MODEL_DIR}/model.ckpt-524288'" \
  --gin_param="utils.tpu_mesh_shape.model_parallelism = 1" \
  --gin_param="utils.tpu_mesh_shape.tpu_topology = '2x2'"
```

## Released Model Checkpoints

We have released the following checkpoints for pre-trained models described in our [paper][paper]:

* **T5-Small** (60 million parameters): [gs://t5-data/pretrained_models/small](https://console.cloud.google.com/storage/browser/t5-data/pretrained_models/small/)
* **T5-Base** (220 million parameters): [gs://t5-data/pretrained_models/base](https://console.cloud.google.com/storage/browser/t5-data/pretrained_models/base/)
* **T5-Large** (770 million parameters): [gs://t5-data/pretrained_models/large](https://console.cloud.google.com/storage/browser/t5-data/pretrained_models/large/)
* **T5-3B** (3 billion parameters): [gs://t5-data/pretrained_models/3B](https://console.cloud.google.com/storage/browser/t5-data/pretrained_models/3B/)
* **T5-11B** (11 billion parameters): [gs://t5-data/pretrained_models/11B](https://console.cloud.google.com/storage/browser/t5-data/pretrained_models/11B/)


# How to cite
If you extend or use this work, please cite the [paper][paper] where it was introduced:

```
@article{2019t5,
  author = {Colin Raffel and Noam Shazeer and Adam Roberts and Katherine Lee and Sharan Narang and Michael Matena and Yanqi Zhou and Wei Li and Peter J. Liu},
  title = {Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer},
  journal = {arXiv e-prints},
  year = {2019},
  archivePrefix = {arXiv},
  eprint = {1910.10683},
}
```

[paper]: https://arxiv.org/abs/1910.10683
[beam]: https://beam.apache.org
[c4]: https://www.tensorflow.org/datasets/catalog/c4
[cc]: https://commoncrawl.org
[dataflow]: https://cloud.google.com/dataflow/
[gcs]: https://www.tensorflow.org/datasets/gcs
[gcd]: https://cloud.google.com/dataflow/
[gin]: https://github.com/google/gin-config
[mtft]: https://github.com/tensorflow/mesh/tree/master/mesh_tensorflow/transformer
[tfds]: https://www.tensorflow.org/datasets
[tfds_beam]: https://www.tensorflow.org/datasets/beam_datasets
