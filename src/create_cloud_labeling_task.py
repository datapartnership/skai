# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=line-too-long

r"""Creates a labeling task on Vertex AI.

This script converts a subset of unlabeled TF examples into labeling image
format (before and after images of each building juxtaposed side-by-side),
uploads them to Vertex AI as a dataset, and creates a labeling job for it. [1]
It will assign the job to the labeler pool that you specify. You can create
labeler pools using the REST API documented at [2].

Example invocation:

python create_cloud_labeling_task.py \
  --cloud_project=my-cloud-project \
  --cloud_location=us-central1 \
  --dataset_name=some_dataset_name \
  --examples_pattern=gs://bucket/disaster/examples/unlabeled-large/*.tfrecord \
  --images_dir=gs://bucket/disaster/examples/labeling-images \
  --max_images=1000 \
  --cloud_labeler_emails=user1@gmail.com,user2@gmail.com

[1] https://cloud.google.com/vertex-ai/docs/datasets/data-labeling-job
[2] https://cloud.google.com/vertex-ai/docs/reference/rest/v1/projects.locations.specialistPools
"""
# pylint: enable=line-too-long


import time

from absl import app
from absl import flags
from absl import logging

from skai import cloud_labeling

FLAGS = flags.FLAGS
flags.DEFINE_string('cloud_project', 'disaster-assessment', 'GCP project name.')
flags.DEFINE_string('cloud_location', 'us-central1', 'Project location.')
flags.DEFINE_string('examples_pattern', '', 'Pattern matching TFRecords.')
flags.DEFINE_string('images_dir', '', 'Directory to write images to.')
flags.DEFINE_integer('max_images', 1000, 'Maximum number of images to label.')
flags.DEFINE_bool('randomize', True, 'If true, randomly sample images.')
flags.DEFINE_string('dataset_name', None, 'Dataset name')
flags.DEFINE_string(
    'cloud_labeler_pool', None, 'Labeler pool. Format is '
    'projects/<project>/locations/us-central1/specialistPools/<id>.')
flags.DEFINE_list(
    'cloud_labeler_emails', None, 'Emails of workers of new labeler pool. '
    'First email will become the manager.')
flags.DEFINE_string('labeler_instructions_uri',
                    'gs://skai-public/labeling_instructions.pdf',
                    'URI for instructions.')

# pylint: disable=line-too-long
flags.DEFINE_string(
    'label_inputs_schema_uri',
    'gs://google-cloud-aiplatform/schema/datalabelingjob/inputs/'
    'image_classification_1.0.0.yaml',
    'Label inputs schema URI. See https://googleapis.dev/python/aiplatform/latest/aiplatform_v1/types.html#google.cloud.aiplatform_v1.types.DataLabelingJob.inputs_schema_uri.')
# pylint: enable=line-too-long


def _get_labeling_dataset_region(project_region: str) -> str:
  """Choose where to host a labeling dataset.

  As of November 2021, labeling datasets can only be created in "us-central1"
  and "europe-west4" regions. See

  https://cloud.google.com/vertex-ai/docs/general/locations#available-regions

  Args:
    project_region: The region of the project.

  Returns:
    Supported region for hosting the labeling dataset.
  """
  if project_region.startswith('europe-'):
    return 'europe-west4'
  return 'us-central1'


def main(unused_argv):
  timestamp = time.strftime('%Y%m%d_%H%M%S')
  timestamped_dataset = f'{FLAGS.dataset_name}_{timestamp}'

  num_images, import_file_path = cloud_labeling.create_labeling_images(
      FLAGS.examples_pattern, FLAGS.max_images, FLAGS.images_dir)
  logging.info('Wrote %d labeling images.', num_images)
  logging.info('Wrote import file %s.', import_file_path)

  if FLAGS.cloud_labeler_pool is not None:
    labeler_pool = FLAGS.cloud_labeler_pool
  else:
    # Create a new labeler pool.
    if not FLAGS.cloud_labeler_emails:
      raise ValueError('Must provide at least one labeler email.')

    pool_display_name = f'{timestamped_dataset}_pool'
    labeler_pool = cloud_labeling.create_specialist_pool(
        FLAGS.cloud_project, FLAGS.cloud_location, pool_display_name,
        FLAGS.cloud_labeler_emails[:1], FLAGS.cloud_labeler_emails)
    logging.log(logging.DEBUG, 'Created labeler pool: %s', labeler_pool)

  cloud_labeling.create_cloud_labeling_job(
      FLAGS.cloud_project,
      _get_labeling_dataset_region(FLAGS.cloud_location),
      timestamped_dataset,
      labeler_pool,
      import_file_path,
      FLAGS.labeler_instructions_uri,
      FLAGS.label_inputs_schema_uri)


if __name__ == '__main__':
  app.run(main)
