# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""The main module that initiates scanning of GCP resources.

"""
import collections
import json
import logging
import os
import sys
from datetime import datetime
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Union, Any

from google.auth.exceptions import MalformedError
from google.cloud import container_v1
from google.cloud import iam_credentials
from google.cloud.iam_credentials_v1.services.iam_credentials.client import IAMCredentialsClient
from googleapiclient import discovery
from httplib2 import Credentials

from . import arguments
from . import credsdb
from .client.client_factory import ClientFactory
from .crawler import misc_crawler
from .crawler.crawler_factory import CrawlerFactory
from .models import SpiderContext

# We define the schema statically to make it easier for the user and avoid extra
# config files.
light_version_scan_schema = {
  'compute_instances': ['name', 'zone', 'machineType', 'networkInterfaces',
                        'status'],
  'compute_images': ['name', 'status', 'diskSizeGb', 'sourceDisk'],
  'machine_images': ['name', 'description', 'status', 'sourceInstance',
                     'totalStorageBytes', 'savedDisks'],
  'compute_disks': ['name', 'sizeGb', 'zone', 'status', 'sourceImage', 'users'],
  'compute_snapshots': ['name', 'status', 'sourceDisk', 'downloadBytes'],
  'managed_zones': ['name', 'dnsName', 'description', 'nameServers'],
  'sql_instances': ['name', 'region', 'ipAddresses', 'databaseVersion',
                    'state'],
  'cloud_functions': ['name', 'eventTrigger', 'status', 'entryPoint',
                      'serviceAccountEmail'],
  'kms': ['name', 'primary', 'purpose', 'createTime'],
  'services': ['name'],
}

# The following map is used to establish the relationship between
# crawlers and clients. It determines the appropriate crawler and
# client to be selected from the respective factory classes.
crawl_client_map = {
  'app_services': 'appengine',
  'bigtable_instances': 'bigtableadmin',
  'bq': 'bigquery',
  'cloud_functions': 'cloudfunctions',
  'compute_disks': 'compute',
  'compute_images': 'compute',
  'compute_instances': 'compute',
  'compute_snapshots': 'compute',
  'dns_policies': 'dns',
  'endpoints': 'servicemanagement',
  'filestore_instances': 'file',
  'firewall_rules': 'compute',
  'iam_policy': 'cloudresourcemanager',
  'kms': 'cloudkms',
  'machine_images': 'compute',
  'managed_zones': 'dns',
  'project_info': 'cloudresourcemanager',
  'pubsub_subs': 'pubsub',
  'services': 'serviceusage',
  'service_accounts': 'iam',
  'sourcerepos': 'sourcerepo',
  'spanner_instances': 'spanner',
  'sql_instances': 'sqladmin',
  'static_ips': 'compute',
  'storage_buckets': 'storage',
  'subnets': 'compute',
}


def is_set(config: Optional[dict], config_setting: str) -> Union[dict, bool]:
  if config is None:
    return True
  obj = config.get(config_setting, {})
  return obj.get('fetch', False)


def save_results(res_data: Dict, res_path: str, is_light: bool):
  """The function to save scan results on disk in json format.

  Args:
    res_data: scan results as a dictionary of entries
    res_path: full path to save data in file
    is_light: save only the most interesting results
  """

  if is_light is True:
    # returning the light version of the scan based on predefined schema
    for gcp_resource, schema in light_version_scan_schema.items():
      projects = res_data.get('projects', {})
      for project_name, project_data in projects.items():
        scan_results = project_data.get(gcp_resource, {})
        light_results = list()
        for scan_result in scan_results:
          light_results.append({key: scan_result.get(key) for key in schema})

        project_data.update({gcp_resource: light_results})
        projects.update({project_name: project_data})
      res_data.update({'projects': projects})

  # Write out results to json DB
  sa_results_data = json.dumps(res_data, indent=2, sort_keys=False)

  with open(res_path, 'a', encoding='utf-8') as outfile:
    outfile.write(sa_results_data)


def crawl_loop(initial_sa_tuples: List[Tuple[str, Credentials, List[str]]],
               out_dir: str,
               scan_config: Dict,
               light_scan: bool,
               target_project: Optional[str] = None,
               force_projects: Optional[str] = None):
  """The main loop function to crawl GCP resources.

  Args:
    initial_sa_tuples: [(sa_name, sa_object, chain_so_far)]
    out_dir: directory to save results
    target_project: project name to scan
    force_projects: a list of projects to force scan
  """

  # Generate current timestamp to append to the filename
  scan_time_suffix = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

  context = SpiderContext(initial_sa_tuples)
  # Main loop
  processed_sas = set()
  while not context.service_account_queue.empty():
    # Get a new candidate service account / token
    sa_name, credentials, chain_so_far = context.service_account_queue.get()
    if sa_name in processed_sas:
      continue

    # Don't process this service account again
    processed_sas.add(sa_name)
    logging.info('>> current service account: %s', sa_name)
    sa_results = infinite_defaultdict()
    # Log the chain we used to get here (even if we have no privs)
    sa_results['service_account_chain'] = chain_so_far
    sa_results['current_service_account'] = sa_name
    # Add token scopes in the result
    sa_results['token_scopes'] = credentials.scopes

    project_list = CrawlerFactory.create_crawler(
      'project_list',
    ).crawl(
      ClientFactory.get_client('cloudresourcemanager').get_service(credentials),
    )
    if len(project_list) <= 0:
      logging.info('Unable to list projects accessible from service account')

    if force_projects:
      for force_project_id in force_projects:
        res = CrawlerFactory.create_crawler(
          'project_info',
        ).crawl(
          force_project_id,
          ClientFactory.get_client('cloudresourcemanager').get_service(
            credentials,
          ),
        )
        if res:
          project_list.append(res)
        else:
          # force object creation anyway
          project_list.append({'projectId': force_project_id,
                               'projectNumber': 'N/A'})

    # Enumerate projects accessible by SA
    for project in project_list:
      if target_project and target_project not in project['projectId']:
        continue

      project_id = project['projectId']
      print(f'Inspecting project {project_id}')
      project_result = sa_results['projects'][project_id]

      project_result['project_info'] = project

      # Fail with error if the output file already exists
      output_file_name = f'{project_id}-{scan_time_suffix}.json'
      output_path = Path(out_dir, output_file_name)
      gcs_output_path = Path(out_dir, f'gcs-{output_file_name}')

      try:
        with open(output_path, 'x', encoding='utf-8'):
          pass

      except FileExistsError:
        logging.error('Try removing the %s file and restart the scanner.',
                      output_file_name)

      for crawler_name, client_name in crawl_client_map.items():
        if is_set(scan_config, crawler_name):
          crawler_config = {}
          if scan_config is not None:
            crawler_config = scan_config.get(crawler_name)
          # add gcs output path to the config.
          # this path is used by the storage bucket crawler as of now.
          crawler_config['gcs_output_path'] = gcs_output_path
          # crawl the data
          crawler = CrawlerFactory.create_crawler(crawler_name)
          client = ClientFactory.get_client(client_name).get_service(
            credentials,
          )
          project_result[crawler_name] = crawler.crawl(
            project_id,
            client,
            crawler_config,
          )

      # Call other miscellaneous crawlers here
      if is_set(scan_config, 'gke_clusters'):
        gke_client = gke_client_for_credentials(credentials)
        project_result['gke_clusters'] = misc_crawler.get_gke_clusters(
          project_id,
          gke_client,
        )
      if is_set(scan_config, 'gke_images'):
        project_result['gke_images'] = misc_crawler.get_gke_images(
          project_id,
          credentials.token,
        )

      # Iterate over discovered service accounts by attempting impersonation
      project_result['service_account_edges'] = []
      updated_chain = chain_so_far + [sa_name]

      if scan_config is not None:
        impers = scan_config.get('service_accounts', None)
      else:
        impers = {'impersonate': False}  # do not impersonate by default

      # trying to impersonate SAs within project
      if impers is not None and impers.get('impersonate', False) is True:
        iam_client = iam_client_for_credentials(credentials)
        if is_set(scan_config, 'iam_policy') is False:
          iam_policy = CrawlerFactory.create_crawler('iam_policy').crawl(
            project_id,
            ClientFactory.get_client('cloudresourcemanager').get_service(
              credentials,
            ),
          )

        project_service_accounts = get_sas_for_impersonation(iam_policy)
        for candidate_service_account in project_service_accounts:
          try:
            logging.info('Trying %s', candidate_service_account)
            creds_impersonated = credsdb.impersonate_sa(
              iam_client, candidate_service_account)
            context.service_account_queue.put(
              (candidate_service_account, creds_impersonated, updated_chain))
            project_result['service_account_edges'].append(
              candidate_service_account)
            logging.info('Successfully impersonated %s using %s',
                         candidate_service_account, sa_name)
          except Exception:
            logging.error('Failed to get token for %s',
                          candidate_service_account)
            logging.error(sys.exc_info()[1])

      logging.info('Saving results for %s into the file', project_id)

      save_results(sa_results, output_path, light_scan)
      # Clean memory to avoid leak for large amount projects.
      sa_results.clear()


def iam_client_for_credentials(
  credentials: Credentials) -> IAMCredentialsClient:
  return iam_credentials.IAMCredentialsClient(credentials=credentials)


def compute_client_for_credentials(
  credentials: Credentials) -> discovery.Resource:
  return discovery.build(
    'compute', 'v1', credentials=credentials, cache_discovery=False)


def gke_client_for_credentials(
  credentials: Credentials
) -> container_v1.services.cluster_manager.client.ClusterManagerClient:
  return container_v1.services.cluster_manager.ClusterManagerClient(
    credentials=credentials)


def get_sa_details_from_key_files(key_path):
  malformed_keys = []
  sa_details = []
  for keyfile in os.listdir(key_path):
    if not keyfile.endswith('.json'):
      malformed_keys.append(keyfile)
      continue

    full_key_path = os.path.join(key_path, keyfile)
    try:
      account_name, credentials = credsdb.get_creds_from_file(full_key_path)
      if credentials is None:
        logging.error('Failed to retrieve credentials for %s', account_name)
        continue

      sa_details.append((account_name, credentials, []))
    except (MalformedError, JSONDecodeError, Exception):
      malformed_keys.append(keyfile)

  if len(malformed_keys) > 0:
    for malformed_key in malformed_keys:
      logging.error('Failed to parse keyfile: %s', malformed_key)

  return sa_details


def get_sas_for_impersonation(
  iam_policy: List[Dict[str, Any]]) -> List[str]:
  """Extract a list of unique SAs from IAM policy associated with project.

  Args:
    iam_policy: An IAM policy provided by get_iam_policy function.

  Returns:
    A list of service accounts represented as string
  """

  if not iam_policy:
    return []

  list_of_sas = list()
  for entry in iam_policy:
    for sa_name in entry.get('members', []):
      if sa_name.startswith('serviceAccount') and '@' in sa_name:
        account_name = sa_name.split(':')[1]
        if account_name not in list_of_sas:
          list_of_sas.append(account_name)

  return list_of_sas


def infinite_defaultdict():
  """Initialize infinite default.

  Returns:
    DefaultDict
  """
  return collections.defaultdict(infinite_defaultdict)


def main():
  logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)
  logging.getLogger('googleapiclient.http').setLevel(logging.ERROR)

  args = arguments.arg_parser()

  force_projects_list = list()
  if args.force_projects:
    force_projects_list = args.force_projects.split(',')

  logging.basicConfig(level=getattr(logging, args.log_level.upper(), None),
                      format='%(asctime)s - %(levelname)s - %(message)s',
                      datefmt='%Y-%m-%d %H:%M:%S',
                      filename=args.log_file, filemode='a')

  sa_tuples = []
  if args.key_path:
    # extracting SA keys from folder
    sa_tuples.extend(get_sa_details_from_key_files(args.key_path))

  if args.use_metadata:
    # extracting GCP credentials from instance metadata
    account_name, credentials = credsdb.get_creds_from_metadata()
    if credentials is None:
      logging.error('Failed to retrieve credentials from metadata')
    else:
      sa_tuples.append((account_name, credentials, []))

  if args.gcloud_profile_path:
    # extracting GCP credentials from gcloud configs
    auths_list = credsdb.get_account_creds_list(args.gcloud_profile_path)

    for accounts in auths_list:
      for creds in accounts:
        # switch between accounts
        account_name = creds.account_name
        account_creds = creds.creds
        access_token = creds.token

        if args.key_name and args.key_name not in account_name:
          continue

        logging.info('Retrieving credentials for %s', account_name)
        credentials = credsdb.get_creds_from_data(access_token,
                                                  json.loads(account_creds))
        if credentials is None:
          logging.error('Failed to retrieve access token for %s', account_name)
          continue

        sa_tuples.append((account_name, credentials, []))

  if args.access_token_files:
    for access_token_file in args.access_token_files.split(','):
      credentials = credsdb.creds_from_access_token(access_token_file)

      if credentials is None:
        logging.error('Failed to retrieve credentials using token provided')
      else:
        token_file_name = os.path.basename(access_token_file)
        sa_tuples.append((token_file_name, credentials, []))

  if args.refresh_token_files:
    for refresh_token_file in args.refresh_token_files.split(','):
      credentials = credsdb.creds_from_refresh_token(refresh_token_file)

      if credentials is None:
        logging.error('Failed to retrieve credentials using token provided')
      else:
        token_file_name = os.path.basename(refresh_token_file)
        sa_tuples.append((token_file_name, credentials, []))

  scan_config = None
  if args.config_path is not None:
    with open(args.config_path, 'r', encoding='utf-8') as f:
      scan_config = json.load(f)

  crawl_loop(sa_tuples, args.output, scan_config, args.light_scan,
             args.target_project, force_projects_list)
  return 0
