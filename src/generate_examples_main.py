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
r"""Runs example generation pipeline.

Example invocation to run on workstation:

python generate_examples_main.py \
  --before_image_path=/path/to/before_image.tif \
  --after_image_path=/path/to/after_image.tif \
  --aoi_path=/path/to/aoi.geojson \
  --buildings_method=open_street_map \
  --output_dir=/path/to/output \


Example invocation to run on Cloud DataFlow:

python generate_examples_main.py \
  --before_image_path=gs://bucket-name/before_image.tif \
  --after_image_path=gs://bucket-name/after_image.tif \
  --aoi_path=/path/to/aoi.geojson \
  --buildings_method=open_street_map \
  --output_dir=gs://bucket-name/disaster-name \
  --use_dataflow \
  --cloud_project=disaster-assessment \
  --cloud_region=us-west1
"""

import os
import platform
import time
from typing import Dict, List, Tuple
from absl import app
from absl import flags
from absl import logging
import geopandas as gpd
import shapely.geometry
from skai import buildings
from skai import cloud_labeling
from skai import generate_examples
from skai import open_street_map

FLAGS = flags.FLAGS

# General GCP flags.
flags.DEFINE_string('cloud_project', None, 'GCP project name.')
flags.DEFINE_string('cloud_region', None, 'GCP region, e.g. us-central1.')
flags.DEFINE_bool('use_dataflow', False, 'If true, run pipeline on Dataflow.')
flags.DEFINE_string(
    'worker_service_account', None,
    'Service account that will launch Dataflow workers. If unset, workers will '
    'run with the project\'s default Compute Engine service account.')

# Example generation flags.
flags.DEFINE_string('dataset_name', None, 'Dataset name.')
flags.DEFINE_string(
    'before_image_path', None, 'Path of pre-disaster GeoTIFF.', required=True)
flags.DEFINE_string(
    'after_image_path', None, 'Path of post-disaster GeoTIFF.', required=True)
flags.DEFINE_string(
    'aoi_path', None, 'Path to file containing area of interest')
flags.DEFINE_string('output_dir', None, 'Output directory.', required=True)
flags.DEFINE_integer('example_patch_size', 64, 'Image patch size.')
flags.DEFINE_integer('alignment_patch_size', 256,
                     'Patch size used during alignment.')
flags.DEFINE_float(
    'resolution', 0.5,
    'The desired resolution (in m/pixel) of the image patches. If this is '
    'different from the image\'s native resolution, patches will be upsampled '
    'or downsampled.')

flags.DEFINE_integer('output_shards', 20, 'Number of output shards.')
flags.DEFINE_string('dataflow_container_image', None,
                    'The SDK container image to use when running Dataflow.')
flags.DEFINE_list('gdal_env', [],
                  'Environment configuration for GDAL. Comma delimited list '
                  'where each element has the form "var=value".')

# Building discovery flags.
flags.DEFINE_enum('buildings_method', 'file',
                  ['file', 'open_street_map', 'none'],
                  'Building detection method')
flags.DEFINE_string(
    'buildings_file', None, 'Path to file containing building locations. '
    'Supports CSV, shapefile, and GeoJSON.')
flags.DEFINE_string('overpass_url',
                    'https://lz4.overpass-api.de/api/interpreter',
                    'OpenStreetMap Overpass server URL.')

# Flags controlling the ingestion of user-provided labels.
flags.DEFINE_string('labels_file', None,
                    'If specified, read labels for dataset from this file.')
flags.DEFINE_string('label_property', None,
                    'Property to use as label, e.g. "Main_Damag".')
flags.DEFINE_list('label_classes', ['undamaged', 'damaged'],
                  'Names of the label classes.')
flags.DEFINE_integer('num_keep_labeled_examples', 1000, 'Number of labeled '
                     'examples to keep (keeps all if None or 0).')

# Flags controlling the creation of a Cloud labeling task for this dataset.
flags.DEFINE_bool('create_cloud_labeling_task', False,
                  'If true, create Vertex AI labeling task to label random '
                  'subset of examples.')
flags.DEFINE_integer('labeling_patch_size', 256,
                     'Patch size used for labeling.')
flags.DEFINE_integer('num_labeling_examples', 500,
                     'Number of examples to label.')
flags.DEFINE_string('cloud_labeler_pool', None, 'Existing labeler pool.')
flags.DEFINE_list('cloud_labeler_emails', None,
                  'Emails of workers of new labeler pool. '
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

Polygon = shapely.geometry.polygon.Polygon


def get_building_centroids(regions: List[Polygon]) -> List[Tuple[float, float]]:
  """Finds building centroids based on flag settings.

  This function is meant to be called from generate_examples_main.py.

  Args:
    regions: List of polygons of regions to find buildings in.

  Returns:
    List of building centroids in (longitude, latitude) format.

  Raises:
    ValueError if buildings_method flag has unknown value.
  """
  if FLAGS.buildings_method == 'file':
    return buildings.read_buildings_file(FLAGS.buildings_file, regions)
  elif FLAGS.buildings_method == 'open_street_map':
    return open_street_map.get_building_centroids_in_regions(
        regions, FLAGS.overpass_url)

  raise ValueError('Invalid value for "buildings_method" flag.')


def _read_labels_file(
    path: str, label_property: str,
    class_names: List[str]) -> List[Tuple[float, float, float]]:
  """Reads labels from a GIS file.

  If the label is a string, then it is assumed to be the name of a class,
  e.g. "damaged". The example's float-value label is assigned to the index of
  that class name in the "class_names" argument. If the name is not in
  "class_names", the example is dropped.

  If the label is a float or integer, it is read as-is.

  Args:
    path: Path to the file to be read.
    label_property: The property to use as the label, e.g. "Main_Damag".
    class_names: List of classes to be used as examples, e.g.
        ["undamaged", "damaged", "destroyed"].

  Returns:
    List of tuples of the form (longitude, latitude, float label).
  """

  df = gpd.read_file(path).to_crs(epsg=4326)
  coordinates = []
  for _, row in df.iterrows():
    centroid = row.geometry.centroid
    label = row[label_property]
    if isinstance(label, str):
      try:
        float_label = float(class_names.index(label))
      except ValueError:
        # Class is not recognized, so skip this coordinate.
        continue
    elif isinstance(label, (int, float)):
      float_label = float(label)
    else:
      raise ValueError(f'Unrecognized label property type {type(label)}')

    coordinates.append((centroid.x, centroid.y, float_label))

  if FLAGS.num_keep_labeled_examples:
    coordinates = coordinates[:FLAGS.num_keep_labeled_examples]

  logging.info('Read %d labeled coordinates.', len(coordinates))
  return coordinates


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


def _get_gdal_env() -> Dict[str, str]:
  gdal_env = {}
  for setting in FLAGS.gdal_env:
    if '=' not in setting:
      raise ValueError(
          'Each element in the gdal_env flag should have the form "var=value".')
    var, _, value = setting.partition('=')
    gdal_env[var] = value
  return gdal_env


def main(args):
  del args  # unused

  # If using Dataflow, check that the container image is valid.
  dataflow_container_image = FLAGS.dataflow_container_image
  py_version = platform.python_version()[:3]
  if FLAGS.use_dataflow and dataflow_container_image is None:
    if py_version == '3.7':
      dataflow_container_image = 'gcr.io/disaster-assessment/dataflow_3.7_image:latest'
    elif py_version == '3.8':
      dataflow_container_image = 'gcr.io/disaster-assessment/dataflow_3.8_image:latest'
    elif py_version == '3.9':
      dataflow_container_image = 'gcr.io/disaster-assessment/dataflow_3.9_image:latest'
    else:
      raise ValueError('dataflow_container_image must be specified when using '
                       'Dataflow and your Python version != 3.7, 3.8, or 3.9.')

  if not FLAGS.labels_file and FLAGS.buildings_method == 'none':
    raise ValueError('At least labels_file (for labeled examples extraction) '
                     'or buildings_method != none (for unlabeled data) should '
                     'be specified.')
  if FLAGS.buildings_method != 'none':
    aoi = buildings.read_aois(FLAGS.aoi_path)
    building_centroids = get_building_centroids(aoi)
    logging.info('Found %d buildings in area of interest.',
                 len(building_centroids))
  else:
    # Only if one wants to extract labeled examples and labels_file is provided.
    building_centroids = []

  if FLAGS.labels_file:
    labeled_coordinates = _read_labels_file(FLAGS.labels_file,
                                            FLAGS.label_property,
                                            FLAGS.label_classes)
  else:
    labeled_coordinates = []

  gdal_env = _get_gdal_env()
  generate_examples.generate_examples_pipeline(
      FLAGS.before_image_path,
      FLAGS.after_image_path,
      FLAGS.example_patch_size,
      FLAGS.alignment_patch_size,
      FLAGS.labeling_patch_size,
      FLAGS.resolution,
      FLAGS.output_dir,
      FLAGS.output_shards,
      building_centroids,
      labeled_coordinates,
      FLAGS.use_dataflow,
      FLAGS.num_labeling_examples,
      gdal_env,
      dataflow_container_image,
      FLAGS.cloud_project,
      FLAGS.cloud_region,
      FLAGS.worker_service_account)

  if FLAGS.create_cloud_labeling_task:
    if not FLAGS.dataset_name:
      raise ValueError('Dataset name must be specified with "--dataset_name"')
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    timestamped_dataset = f'{FLAGS.dataset_name}_{timestamp}'
    if FLAGS.cloud_labeler_pool is not None:
      labeler_pool = FLAGS.cloud_labeler_pool
    else:
      if not FLAGS.cloud_labeler_emails:
        raise ValueError('Must provide at least one labeler email.')

      pool_display_name = f'{timestamped_dataset}_pool'
      labeler_pool = cloud_labeling.create_specialist_pool(
          FLAGS.cloud_project, FLAGS.cloud_region, pool_display_name,
          FLAGS.cloud_labeler_emails[:1], FLAGS.cloud_labeler_emails)
      logging.log(logging.DEBUG, 'Created labeler pool: %s', labeler_pool)

    import_file_uri = os.path.join(
        FLAGS.output_dir, 'examples', 'labeling_images', 'import_file.csv')
    cloud_labeling.create_cloud_labeling_job(
        FLAGS.cloud_project,
        _get_labeling_dataset_region(FLAGS.cloud_region),
        timestamped_dataset,
        labeler_pool,
        import_file_uri,
        FLAGS.labeler_instructions_uri,
        FLAGS.label_inputs_schema_uri)


if __name__ == '__main__':
  app.run(main)
